#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import atexit
import hashlib
import json
import logging
import os
import random
import re
import signal
import socket
import subprocess
import time
import imaplib
import email
import urllib.request
from email.header import decode_header
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth
from camoufox.async_api import AsyncCamoufox
from browserforge.fingerprints import Screen as CamoufoxScreen
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from customer_db import (
    Customer, add_customer, get_customer, update_customer,
    update_customer_status, delete_customer, search_customers,
    list_customers, customer_count,
)
from optimizer import (
    log_attempt, set_account_cooldown, is_account_on_cooldown,
    get_cooldown_remaining, rank_proxies, rank_accounts,
    best_cf_wait_secs, stats_summary,
)
from auto_booking import AutoBookManager


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "state.json"
SITES_FILE = BASE_DIR / "sites.json"
PROXIES_FILE = BASE_DIR / "proxies.txt"
VFS_OPTIONS_CACHE_FILE = DATA_DIR / "vfs_options_cache.json"
COOKIES_DIR = DATA_DIR / "cookies"

CLOSED_COOLDOWN_SECS = 600  # CLOSED sonuç sonrası yeni sorgu için minimum bekleme (10 dakika)

# ── Anti-Detection: Yardımcılar ──────────────────────────────────────────────

def _jitter(base: float, pct: float = 0.25) -> float:
    """base süresine ±pct kadar rastgele sapma ekler. Minimum 0.1s."""
    delta = base * pct
    return max(0.1, base + random.uniform(-delta, delta))

async def _sleep(base: float, pct: float = 0.25) -> None:
    """Jitter'lı asyncio.sleep."""
    await asyncio.sleep(_jitter(base, pct))

# Gerçekçi Chrome/Firefox User-Agent havuzu (Windows + macOS karışık)
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6084.168 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# Gerçekçi viewport boyutları (1080p, 1440p, laptop ekranları)
_VIEWPORT_POOL = [
    (1920, 1080), (1920, 1080), (1920, 1080),  # en yaygın 3x ağırlık
    (1366, 768),  (1366, 768),  (1366, 768),   # laptop 3x ağırlık
    (1440, 900),  (1440, 900),
    (1536, 864),
    (1600, 900),
    # NOT: 1280px'in altı VFS Angular layout bozabilir — eklenmedi
]

# Hesap başına rate-limit sayacı → adaptive cooldown için
_account_rl_count: dict[str, int] = {}

# Hesap başına son başarılı kullanım zamanı → minimum kullanım aralığı için
_account_last_used: dict[str, float] = {}

# Aynı hesabın arka arkaya kullanılabileceği minimum bekleme süresi (saniye)
# Session cookie geçerliyse kısa (30s), tam login gerekiyorsa uzun (15dk)
_ACCOUNT_MIN_INTERVAL_SESSION = 30    # Cookie ile hızlı sorgu
_ACCOUNT_MIN_INTERVAL_LOGIN    = 900   # Tam login (15 dakika)


def _cookies_path(email: str) -> Path:
    """Email adresine göre session cookie dosya yolu."""
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    safe = hashlib.md5(email.encode()).hexdigest()[:12]
    return COOKIES_DIR / f"vfs_{safe}.json"


async def _human_move_to(page, tx: float, ty: float) -> None:
    """Mouse'u Bezier eğrisiyle insan gibi hedef koordinata taşır."""
    try:
        vp = page.viewport_size or {}
        sx = random.uniform(100, vp.get("width", 1366) * 0.6)
        sy = random.uniform(80,  vp.get("height", 768) * 0.5)
        cp1x = sx + (tx - sx) * random.uniform(0.2, 0.4) + random.uniform(-70, 70)
        cp1y = sy + (ty - sy) * random.uniform(0.2, 0.4) + random.uniform(-50, 50)
        cp2x = sx + (tx - sx) * random.uniform(0.6, 0.8) + random.uniform(-40, 40)
        cp2y = sy + (ty - sy) * random.uniform(0.6, 0.8) + random.uniform(-30, 30)
        steps = random.randint(18, 30)
        for i in range(steps + 1):
            t = i / steps
            mx = (1-t)**3*sx + 3*(1-t)**2*t*cp1x + 3*(1-t)*t**2*cp2x + t**3*tx
            my = (1-t)**3*sy + 3*(1-t)**2*t*cp1y + 3*(1-t)*t**2*cp2y + t**3*ty
            await page.mouse.move(mx, my)
            await asyncio.sleep(random.uniform(0.008, 0.032))
    except Exception as _e:
        logger.debug(f"_human_move_to hata: {_e}")

def _adaptive_cooldown_secs(email: str) -> int:
    """Rate limit yiyen hesap için katlanarak artan cooldown (300s → 600 → 1200 → 2400 max)."""
    count = _account_rl_count.get(email, 0) + 1
    _account_rl_count[email] = count
    return min(300 * (2 ** (count - 1)), 2400)  # 5dk, 10dk, 20dk, 40dk (max)

# ─────────────────────────────────────────────────────────────────────────────

load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Yetkili Telegram kullanıcı ID'leri — sadece bu kişiler bota erişebilir
_raw_allowed = os.getenv("ALLOWED_USERS", "5816716319,8055800757")
ALLOWED_USERS: set[int] = {int(x.strip()) for x in _raw_allowed.split(",") if x.strip()}

# Admin Telegram ID — sistem mesajları ve ekran görüntüleri buraya gider
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "5816716319"))

# Bot referansı — admin'e direkt mesaj/fotoğraf göndermek için
_bot_ref: list = [None]  # [bot_instance]

async def send_admin_photo(path: str, caption: str = "") -> None:
    """Ekran görüntüsünü sadece loglar (Telegram'a gönderilmez)."""
    logger.info(f"Admin photo (gönderilmedi): {path}")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[%(asctime)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("visa-bot")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_vfs_options_cache() -> dict:
    try:
        if VFS_OPTIONS_CACHE_FILE.exists():
            return json.loads(VFS_OPTIONS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_vfs_options_cache(data: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        VFS_OPTIONS_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"VFS options cache yazılamadı: {e}")


def load_sites() -> list[dict]:
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── SOCKS5 Tünel Yöneticisi ────────────────────────────────────────────────
# Firefox/Camoufox kimlik doğrulamalı SOCKS5'i desteklemez.
# Çözüm: gost ile yerel kimliksiz SOCKS5 tüneli açıp ona yönlendirme.

GOST_BIN = BASE_DIR / "gost"
_socks5_tunnels: dict[str, dict] = {}  # remote_key → {port, proc}


def _free_local_port() -> int:
    """Boş bir yerel port numarası döndürür."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cleanup_stale_gost_tunnels() -> None:
    """Bot başlangıcında sistemde kalan eski gost süreçlerini temizler."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(GOST_BIN)],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p.strip()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if pids:
            time.sleep(0.5)
            logger.info(f"Başlangıç temizliği: {len(pids)} eski gost süreci sonlandırıldı.")
    except Exception as e:
        logger.warning(f"Gost başlangıç temizleme hatası: {e}")


def _cleanup_all_tunnels() -> None:
    """Tüm aktif gost tünellerini kapatır (atexit/shutdown için)."""
    for key, info in list(_socks5_tunnels.items()):
        proc = info.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    _socks5_tunnels.clear()


atexit.register(_cleanup_all_tunnels)


def _refresh_socks5_proxy(proxy: dict) -> dict:
    """Yerel SOCKS5 tüneli ölmüşse yeniden açar ve güncel proxy dict'i döndürür."""
    if not proxy or "_socks5" not in proxy:
        return proxy
    ip, port, user, pwd = proxy["_socks5"]
    key = f"{user}@{ip}:{port}"
    existing = _socks5_tunnels.get(key, {})
    proc = existing.get("proc")
    if proc and proc.poll() is None:
        # Tünel hâlâ çalışıyor
        return {**proxy, "server": f"socks5://127.0.0.1:{existing['port']}"}
    # Tünel ölmüş — yeniden aç
    logger.warning(f"SOCKS5 tüneli ölmüş, yeniden açılıyor: {key}")
    new_port = _ensure_socks5_tunnel(ip, port, user, pwd)
    return {**proxy, "server": f"socks5://127.0.0.1:{new_port}"}


def _ensure_socks5_tunnel(remote_host: str, remote_port: str, user: str, pwd: str) -> int:
    """
    Kimlik doğrulamalı SOCKS5 proxy için yerel gost tüneli açar.
    Tünel zaten çalışıyorsa mevcut yerel portu döndürür.
    """
    key = f"{user}@{remote_host}:{remote_port}"
    if key in _socks5_tunnels:
        proc = _socks5_tunnels[key]["proc"]
        if proc.poll() is None:  # Hâlâ çalışıyor
            return _socks5_tunnels[key]["port"]
        # Ölü süreç — kayıttan temizle, yenisini aç
        _socks5_tunnels.pop(key, None)
    local_port = _free_local_port()
    gost_cmd = [
        str(GOST_BIN),
        "-L", f"socks5://127.0.0.1:{local_port}",
        "-F", f"socks5://{user}:{pwd}@{remote_host}:{remote_port}",
    ]
    if not GOST_BIN.exists():
        logger.warning(f"gost bulunamadı: {GOST_BIN} — SOCKS5 tüneli açılamıyor")
        return int(remote_port)
    proc = subprocess.Popen(gost_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)  # gost'un ayağa kalkması için kısa bekleme
    _socks5_tunnels[key] = {"port": local_port, "proc": proc}
    logger.info(f"SOCKS5 tünel açıldı: 127.0.0.1:{local_port} → {key} (PID: {proc.pid})")
    return local_port


def load_proxies() -> list[dict]:
    proxies = []
    if PROXIES_FILE.exists():
        with open(PROXIES_FILE, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split(":")
                if len(parts) == 5:
                    ip, port, user, pwd, proto = parts
                    scheme = proto.strip().lower() if proto.strip() else "http"
                    if scheme == "socks5":
                        # Firefox/Camoufox auth SOCKS5 desteklemez → gost tüneli aç
                        local_port = _ensure_socks5_tunnel(ip, port, user, pwd)
                        proxies.append({"server": f"socks5://127.0.0.1:{local_port}", "_socks5": (ip, port, user, pwd)})
                    else:
                        proxies.append(
                            {
                                "server": f"{scheme}://{ip}:{port}",
                                "username": user,
                                "password": pwd,
                            }
                        )
                elif len(parts) == 4:
                    ip, port, user, pwd = parts
                    proxies.append(
                        {
                            "server": f"http://{ip}:{port}",
                            "username": user,
                            "password": pwd,
                        }
                    )
                elif len(parts) == 2:
                    ip, port = parts
                    proxies.append({"server": f"http://{ip}:{port}"})
    return proxies


def _parse_proxy_line(line: str) -> dict | None:
    """proxies.txt formatındaki proxy satırını dict'e çevirir.
    Format: ip:port:user:pass:proto  veya  ip:port:user:pass  veya  ip:port
    """
    parts = line.strip().split(":")
    if len(parts) == 5:
        ip, port, user, pwd, proto = parts
        scheme = proto.strip().lower() or "http"
        if scheme == "socks5":
            local_port = _ensure_socks5_tunnel(ip, port, user, pwd)
            return {"server": f"socks5://127.0.0.1:{local_port}", "_socks5": (ip, port, user, pwd)}
        return {"server": f"{scheme}://{ip}:{port}", "username": user, "password": pwd}
    elif len(parts) == 4:
        ip, port, user, pwd = parts
        return {"server": f"http://{ip}:{port}", "username": user, "password": pwd}
    elif len(parts) == 2:
        ip, port = parts
        return {"server": f"http://{ip}:{port}"}
    return None


def status_emoji(label: str) -> str:
    return {
        "OPEN": "✅",
        "CLOSED": "❌",
        "ERROR": "⚠️",
        "UNKNOWN": "🟡",
        "PENDING": "🕐",
        "BOOKED": "🎉",
        "PAYMENT": "💳",
    }.get(label, "🟡")


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def extract_email_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            if content_type in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(errors="ignore") + "\n"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")
    return body


def get_vfs_otp_sync(email_user: str, email_pass: str, since_timestamp: float = None) -> Optional[str]:
    import email.utils as email_utils
    mail = None
    try:
        logger.info("IMAP bağlantısı kuruluyor...")
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_user, email_pass)
        mail.select("INBOX")

        status, data = mail.search(None, "(UNSEEN)")
        ids = data[0].split() if status == "OK" and data and data[0] else []

        if not ids:
            status, data = mail.search(None, "ALL")
            ids = data[0].split()[-30:] if status == "OK" and data and data[0] else []

        if not ids:
            logger.warning("Inbox içinde aday mail yok.")
            return None

        candidate_ids = ids[-30:]
        otp_pattern = re.compile(r"\b(\d{6})\b")

        for msg_id in reversed(candidate_ids):
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            for part in msg_data:
                if not isinstance(part, tuple):
                    continue

                msg = email.message_from_bytes(part[1])

                # Zaman filtresi: sadece login girişiminden sonra gelen mailler
                if since_timestamp is not None:
                    date_str = msg.get("Date", "")
                    try:
                        email_dt = email_utils.parsedate_to_datetime(date_str)
                        email_ts = email_dt.timestamp()
                        if email_ts < since_timestamp - 30:
                            logger.debug(f"Eski mail atlandı: {date_str}")
                            continue
                    except Exception:
                        pass  # Tarih okunamazsa devam et

                msg_from = decode_mime_header(msg.get("From", "")).lower()
                subject = decode_mime_header(msg.get("Subject", "")).lower()
                body = extract_email_body(msg).lower()

                looks_relevant = any(
                    x in (msg_from + " " + subject + " " + body)
                    for x in ["vfs", "vfsglobal", "otp", "verification", "one time password"]
                )
                if not looks_relevant:
                    continue

                match = otp_pattern.search(body)
                if match:
                    otp = match.group(1)
                    logger.info(f"OTP bulundu: {otp} (mail tarihi: {msg.get('Date', '?')})")
                    return otp

    except Exception as e:
        logger.error(f"IMAP Hatası: {e}")
    finally:
        if mail is not None:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass

    return None


async def wait_first_visible(page, selectors: list[str], timeout_ms: int = 15000) -> Optional[str]:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    return sel
            except Exception:
                pass
        await _sleep(0.5)
    return None


async def click_first_visible(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click()
                return True
        except Exception:
            pass
    return False


async def fill_first_visible(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                # Mouse'u input'a insan gibi taşı, sonra tıkla
                try:
                    _bb = await loc.bounding_box()
                    if _bb:
                        await _human_move_to(page, _bb["x"] + _bb["width"] / 2, _bb["y"] + _bb["height"] / 2)
                except Exception:
                    pass
                await loc.click()
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await loc.fill("")
                # Karakter karakter, değişken hızda yaz
                for ch in str(value):
                    await page.keyboard.type(ch)
                    delay = random.uniform(0.04, 0.13)
                    if random.random() < 0.06:   # %6 ihtimalle düşünme duraklaması
                        delay += random.uniform(0.2, 0.6)
                    await asyncio.sleep(delay)
                return True
        except Exception:
            pass
    return False


def _md_escape(text: str) -> str:
    """Telegram Markdown v1 için özel karakterleri escape et."""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def format_message(site: dict, result: dict, check_count: int, dynamic_config: dict | None = None) -> str:
    icon = status_emoji(result["status_label"])
    checked_at = time.strftime("%d %b %Y %H:%M:%S")

    center = _md_escape((dynamic_config or {}).get("center_text") or site.get("center_name", site.get("city", "-")))
    category = _md_escape((dynamic_config or {}).get("category_text") or site.get("visa_category", "-"))
    subcategory = _md_escape((dynamic_config or {}).get("subcategory_text") or site.get("visa_type", "-"))
    mission = _md_escape(site.get("mission_code", "-"))
    matched = _md_escape(result.get("matched_text", "-"))

    return (
        f"*{icon} YENİ RANDEVU DURUMU!*\n\n"
        f"🏢 *Merkez:* {center}\n"
        f"🌍 *Ülke/Misyon:* {mission}\n"
        f"🛂 *Kategori:* {category}\n"
        f"📄 *Tip:* {subcategory}\n"
        f"🚦 *Durum:* {icon} {result['status_label'].lower()}\n"
        f"🗓️ *Son Müsait Tarih:* {matched}\n\n"
        f"📊 *Takip Sayısı:* {check_count}\n\n"
        f"⏰ *Son Kontrol:* {checked_at}"
    )


async def detect_non_vfs_status(page, site: dict) -> dict:
    body_text = await page.locator("body").inner_text()
    body_lower = body_text.lower()

    for phrase in site.get("closed_texts", []):
        if phrase.lower() in body_lower:
            return {
                "ok": True,
                "status_label": "CLOSED",
                "matched_text": "Yok",
                "signature": hashlib.sha256(f"closed:{phrase}".encode()).hexdigest()[:12],
            }

    for phrase in site.get("open_texts", []):
        if phrase.lower() in body_lower:
            return {
                "ok": True,
                "status_label": "OPEN",
                "matched_text": "Form erişilebilir",
                "signature": hashlib.sha256(f"open:{phrase}".encode()).hexdigest()[:12],
            }

    try:
        form_count = await page.locator("input, select, textarea, button").count()
        if form_count >= 3:
            return {
                "ok": True,
                "status_label": "OPEN",
                "matched_text": "Form erişilebilir",
                "signature": hashlib.sha256(f"open:form:{form_count}".encode()).hexdigest()[:12],
            }
    except Exception:
        pass

    return {
        "ok": True,
        "status_label": "UNKNOWN",
        "matched_text": "-",
        "signature": hashlib.sha256(body_text.encode()).hexdigest()[:12],
    }


async def fetch_site(site: dict[str, Any], proxies: list[dict], dynamic_config: dict | None = None, notify_func=None, booking_mode: bool = False, wait_input_func=None, resume_mode: bool = False) -> dict[str, Any]:
    timeout_ms = int(site.get("timeout_ms", 60000))
    provider = site.get("provider", "").upper()

    use_proxy = site.get("use_proxy", provider == "VFS")

    # Aktif hesap — VFS retry loop tarafından güncellenir
    _active_account: dict = {
        "email": (dynamic_config or {}).get("email", ""),
        "password": (dynamic_config or {}).get("password", ""),
        "imap_password": (dynamic_config or {}).get("imap_password", ""),
    }

    # VFS için proxy seçimi retry loopunda yapılır; diğerleri için şimdi seç
    if provider != "VFS":
        proxy = random.choice(proxies) if (proxies and use_proxy) else None
        if proxy:
            logger.info(f"Proxy kullanılıyor: {proxy.get('server')}")
    else:
        proxy = None  # VFS retry loop tarafından ayarlanacak

    async def _run_page(page) -> dict:
        async def save_error_screenshot(tag: str):
            filename = BASE_DIR / f"hata_ekrani_{int(time.time())}_{tag}.png"
            try:
                await page.screenshot(path=str(filename), full_page=True)
                logger.error(f"Ekran görüntüsü kaydedildi: {filename}")
            except Exception as e:
                logger.error(f"Ekran görüntüsü alınamadı: {e}")

        try:
            page.on(
                "response",
                lambda response: logger.info(
                    f"HTTP {response.status} -> {response.url}"
                    if any(x in response.url.lower() for x in ["login", "otp", "verify", "appointment", "dashboard"])
                    else ""
                ),
            )
            page.on(
                "requestfailed",
                lambda request: logger.error(f"REQ FAILED {request.url} {request.failure}")
            )

            # Failed liftassets URL'lerini yakala (CF engeli için)
            failed_liftassets_urls: list = []

            _orig_req_failed = None

            def _on_req_failed(request):
                if 'liftassets.vfsglobal.com' in request.url:
                    if request.url not in failed_liftassets_urls:
                        failed_liftassets_urls.append(request.url)

            page.on("requestfailed", _on_req_failed)

            # VFS lift-api kategori/alt kategori/uyruk API yanıtlarını yakala
            _vfs_api_cats: list[dict] = []
            _vfs_api_subcats: list[dict] = []
            _vfs_api_nationalities: list[str] = []
            _vfs_api_headers: dict = {}  # clientsource ve route header'larını sakla

            async def _on_vfs_request(request):
                """lift-api isteklerinden authorize/clientsource/route headerlarını yakala."""
                if 'lift-api.vfsglobal.com' not in request.url:
                    return
                try:
                    hdrs = request.headers
                    # clientsource her requestte değişir — her zaman güncelle
                    if hdrs.get("clientsource"):
                        _vfs_api_headers["clientsource"] = hdrs["clientsource"]
                    # authorize her zaman güncelle — eski token 401'e yol açar
                    if hdrs.get("authorize"):
                        _vfs_api_headers["authorize"] = hdrs["authorize"]
                        logger.info(f"VFS authorize header güncellendi: {hdrs['authorize'][:30]}...")
                    if hdrs.get("route"):
                        _vfs_api_headers["route"] = hdrs["route"]
                    if hdrs.get("accept"):
                        _vfs_api_headers["accept"] = hdrs["accept"]
                except Exception:
                    pass

            page.on("request", _on_vfs_request)

            async def _capture_vfs_api(response):
                url = response.url
                if 'lift-api.vfsglobal.com' not in url:
                    return
                try:
                    # Her başarılı lift-api isteğini logla
                    if response.status == 200:
                        logger.info(f"VFS API 200: {url}")
                    # Sadece JSON içerebilecek endpointleri parse et
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct and "text" not in ct:
                        return
                    data = await response.json()
                    if not isinstance(data, (list, dict)):
                        return
                    items = data if isinstance(data, list) else (
                        data.get("data") or data.get("results") or data.get("categories") or []
                    )
                    if not items or not isinstance(items, list) or not isinstance(items[0], dict):
                        return
                    first = items[0]
                    url_lower = url.lower()
                    # URL bazlı tespite bak (en güvenilir)
                    # ÖNEMLİ: subvisacategory'yi ÖNCE kontrol et — yoksa "visacategory" match eder
                    if "subvisacategory" in url_lower or "sub-category" in url_lower or "visasubcategory" in url_lower:
                        logger.info(f"✅ VFS alt kategori API yakalandı ({len(items)} adet): {url}")
                        logger.info(f"   İlk öğe keys: {list(first.keys())}")
                        _vfs_api_subcats.clear()
                        _vfs_api_subcats.extend(items)
                        return
                    if "visacategory" in url_lower or "visa-category" in url_lower:
                        logger.info(f"✅ VFS kategori API yakalandı ({len(items)} adet): {url}")
                        logger.info(f"   İlk öğe keys: {list(first.keys())}")
                        _vfs_api_cats.clear()
                        _vfs_api_cats.extend(items)
                        return
                    # Data key bazlı tespit (fallback)
                    keys_str = " ".join(first.keys()).lower()
                    if any(k in keys_str for k in ("visacategorycode", "categorycode", "visa_category")):
                        logger.info(f"✅ VFS kategori API (key match) yakalandı ({len(items)} adet): {url}")
                        _vfs_api_cats.clear()
                        _vfs_api_cats.extend(items)
                    elif any(k in keys_str for k in ("subcategorycode", "sub_category", "visasubcategory")):
                        logger.info(f"✅ VFS alt kategori API (key match) yakalandı ({len(items)} adet): {url}")
                        _vfs_api_subcats.clear()
                        _vfs_api_subcats.extend(items)
                    elif any(k in url_lower for k in ("nationality", "nationalit", "country")) and \
                            any(k in keys_str for k in ("nationality", "nationalityname", "countryname", "country_name", "nationalitycode")):
                        # Uyruk listesi — değerleri string olarak çıkar
                        name_key = next((k for k in first.keys() if "name" in k.lower()), None) or list(first.keys())[0]
                        names = [item.get(name_key, "") for item in items if item.get(name_key)]
                        if names:
                            logger.info(f"✅ VFS uyruk API yakalandı ({len(names)} adet): {url}")
                            _vfs_api_nationalities.clear()
                            _vfs_api_nationalities.extend(names)
                            # Anlık olarak cache'e yaz
                            cache = load_vfs_options_cache()
                            cache["nationalities"] = names
                            cache["nationalities_updated"] = int(time.time())
                            save_vfs_options_cache(cache)
                except Exception as _e:
                    logger.debug(f"VFS API parse hatası {url}: {_e}")

            page.on("response", _capture_vfs_api)

            # Cloudflare tam-sayfa Turnstile / interstitial challenge tespiti ve çözümü
            async def handle_cf_interstitial():
                # Cloudflare interstitial sayfasını gösteren anahtar kelimeler
                cf_keywords = [
                    "güvenlik doğrulaması",
                    "gerçek kişi olduğunuzu doğrulayın",
                    "bir dakika lütfen",    # CF TR: "One minute please"
                    "bir dakika",
                    "just a moment",
                    "checking your browser",
                    "security check",
                    "verify you are human",
                    "please wait",
                    "challenge",
                ]
                import re as _re
                _queue_it_count = [0]
                _queue_it_notified = [False]

                for cf_wait in range(30):
                    # Önce CF frame var mı kontrol et (body text'ten daha güvenilir)
                    cf_frame = next(
                        (f for f in page.frames if 'challenges.cloudflare.com' in f.url),
                        None
                    )

                    try:
                        body_text = (await page.locator("body").inner_text(timeout=5000)).lower()
                    except Exception:
                        body_text = ""

                    queue_it_keywords = [
                        "estimated wait time",
                        "virtual queue",
                        "high volume of traffic",
                        "you are in the queue",
                        "queue-it",
                        "queueit",
                        "this page will automatically refresh",
                    ]
                    if any(kw in body_text for kw in queue_it_keywords):
                        _queue_it_count[0] += 1
                        m = _re.search(r'estimated wait time is (\d+) minute', body_text)
                        wait_min = int(m.group(1)) if m else 2
                        logger.warning(f"Queue-it kuyruğu tespit edildi ({_queue_it_count[0]}. kez) — bekleme: {wait_min} dakika")
                        if _queue_it_count[0] >= 4:
                            logger.warning("Queue-it 4 kez tekrarlandı — rate_limited olarak işaretleniyor")
                            raise Exception("queue_it_max_retries")
                        if not _queue_it_notified[0] and notify_func is not None:
                            try:
                                await notify_func(
                                    f"⏳ *VFS Kuyruk Bekleme*\n\n"
                                    f"Siteye giriş için *{wait_min} dakikalık* kontrol sırası bekleniyor.\n"
                                    f"Sıra bitince sorgu otomatik devam edecek..."
                                )
                            except Exception:
                                pass
                            _queue_it_notified[0] = True
                        wait_sec = max(wait_min * 60, 30)
                        logger.info(f"Queue-it: {wait_sec}s bekleniyor, sonra URL yeniden yüklenecek")
                        await asyncio.sleep(wait_sec)
                        try:
                            await page.goto(site["url"], wait_until="domcontentloaded", timeout=30000)
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        continue

                    on_cf_page = cf_frame is not None or any(kw in body_text for kw in cf_keywords)
                    if not on_cf_page:
                        logger.info(f"CF interstitial geçildi ({cf_wait+1}. deneme sonrası)")
                        return

                    # 15. denemede hâlâ CF'deyse: IP rotate et, browser'ı kapat, komple resetle
                    if cf_wait == 14:
                        _rot_url = ""
                        try:
                            _rot_file = DATA_DIR / "proxy_rotation.json"
                            if _rot_file.exists():
                                _rot_data = json.loads(_rot_file.read_text(encoding="utf-8"))
                                if _rot_data:
                                    _rot_url = list(_rot_data.values())[0].get("url", "")
                        except Exception:
                            pass
                        if _rot_url:
                            logger.warning(f"CF 15 denemede geçilemedi — IP+browser sıfırlanıyor: {_rot_url}")
                            try:
                                await asyncio.to_thread(urllib.request.urlopen, _rot_url, timeout=15)
                                logger.info("IP rotasyonu başarılı")
                                if notify_func:
                                    try:
                                        await notify_func("🔄 CF engeli — IP+browser sıfırlanıyor, otomatik tekrar deneniyor...")
                                    except Exception:
                                        pass
                            except Exception as _re:
                                logger.warning(f"IP rotasyon isteği başarısız: {_re}")
                        else:
                            logger.warning("CF 15 denemede geçilemedi — rotation URL yok, browser sıfırlanıyor")
                        raise Exception("cf_ip_rotated")

                    if cf_frame:
                        try:
                            fe = await cf_frame.frame_element()
                            bbox = await fe.bounding_box()
                            if bbox and bbox.get('width', 0) > 0:
                                # Frame görününce insan gibi 1-3s bak, sonra tıkla
                                await asyncio.sleep(random.uniform(1.0, 3.0))
                                click_x = bbox['x'] + min(22, bbox['width'] / 4) + random.uniform(-3, 3)
                                click_y = bbox['y'] + bbox['height'] / 2 + random.uniform(-2, 2)
                                logger.info(f"CF interstitial tıklanıyor: ({click_x:.0f}, {click_y:.0f})")
                                await _human_move_to(page, click_x, click_y)
                                await asyncio.sleep(random.uniform(0.1, 0.3))
                                await page.mouse.click(click_x, click_y)
                                await _sleep(random.uniform(6.0, 12.0), pct=0.15)
                                continue
                        except Exception as ex:
                            logger.debug(f"CF interstitial tıklama hatası: {ex}")
                    else:
                        logger.info(f"CF interstitial: frame yok, bekleniyor ({cf_wait+1}/30)...")

                    await _sleep(3)

                logger.warning("CF interstitial 30 denemede geçilemedi")
                raise Exception("cf_stuck")

            async def visit_and_solve_cf(url: str, timeout_ms_local: int = 30000):
                """Verilen URL'yi ziyaret et ve CF interstitial varsa çöz (cookie ısıtma)."""
                logger.info(f"CF ısıtma ziyareti: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms_local)
                except Exception:
                    pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await handle_cf_interstitial()
                await _sleep(2)

            if provider != "VFS":
                logger.info(f"Site açılıyor: {site['url']}")
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                await _sleep(5)
                return await detect_non_vfs_status(page, site)

            logger.info("VFS login akışı başlatılıyor...")

            # === SESSION YENİDEN KULLANIMI (Cookie Persistence) ===
            _cpf = _cookies_path(_active_account["email"])
            _session_restored = False
            if _cpf.exists():
                _age = time.time() - _cpf.stat().st_mtime
                if _age < 21600:  # 6 saat geçerliliği (VFS cookie'leri uzun ömürlü)
                    try:
                        _sc = json.loads(_cpf.read_text(encoding="utf-8"))
                        await page.context.add_cookies(_sc)
                        # Login URL'den dashboard URL'yi türet
                        _du = re.sub(r'/(login|signin|sign-in)[^/]*$', '/dashboard', site["url"], flags=re.IGNORECASE)
                        if _du == site["url"]:
                            _du = site["url"].rstrip("/").rsplit("/", 1)[0] + "/dashboard"
                        logger.info(f"Kaydedilmiş session deneniyor ({_age:.0f}s): {_active_account['email']}")
                        try:
                            await page.goto(_du, wait_until="domcontentloaded", timeout=20000)
                        except Exception:
                            await page.goto(site["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        await _sleep(3)
                        _sb = (await page.locator("body").inner_text())[:500].lower()
                        if any(x in _sb for x in ["start new booking", "sign out", "new application", "active application"]):
                            logger.info(f"✅ Session yeniden kullanıldı — login atlandı: {_active_account['email']}")
                            _session_restored = True
                        else:
                            logger.info("Session geçersiz veya süresi dolmuş — normal login")
                            _cpf.unlink(missing_ok=True)
                            try:
                                await page.context.clear_cookies()
                            except Exception:
                                pass
                    except Exception as _ce:
                        logger.warning(f"Session yükleme hatası: {_ce}")
                        try:
                            _cpf.unlink(missing_ok=True)
                        except Exception:
                            pass
                else:
                    logger.info(f"Session dosyası eski ({_age:.0f}s > 21600s), siliniyor")
                    _cpf.unlink(missing_ok=True)

            # Ana login sayfasını aç, CF interstitial'ı çöz, sonra Angular kaynaklarını ısıt
            async def load_vfs_login_page() -> bool:
                """True → sayfa başarılı yüklendi, False → liftassets proxy hatası"""
                # Önce VFS ana/public sayfasını ziyaret et — gerçek kullanıcı navigasyon geçmişi
                _base_url = re.sub(r'/(login|signin|sign-in)[^/]*$', '', site["url"], flags=re.IGNORECASE).rstrip("/")
                if _base_url and _base_url != site["url"].rstrip("/"):
                    # Ana sayfa ziyareti
                    try:
                        logger.info(f"Pre-login ziyaret (1/3): {_base_url}")
                        await page.goto(_base_url, wait_until="domcontentloaded", timeout=20000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        await handle_cf_interstitial()
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        scroll_y = random.randint(80, 350)
                        await page.evaluate(f"window.scrollBy(0, {scroll_y})")
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                        await page.evaluate(f"window.scrollBy(0, -{scroll_y // 2})")
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                    except Exception as _pre_e:
                        logger.debug(f"Pre-login ziyaret (1) atlandı: {_pre_e}")

                    # VFS country seçim sayfası ziyareti — gerçek kullanıcı buradan gelir
                    _country_url = re.sub(r'/tur/.*$', '', site["url"]).rstrip("/")
                    if _country_url and _country_url != _base_url:
                        try:
                            logger.info(f"Pre-login ziyaret (2/3): {_country_url}")
                            await page.goto(_country_url, wait_until="domcontentloaded", timeout=15000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=6000)
                            except Exception:
                                pass
                            await handle_cf_interstitial()
                            await asyncio.sleep(random.uniform(1.0, 2.5))
                            scroll_y2 = random.randint(60, 200)
                            await page.evaluate(f"window.scrollBy(0, {scroll_y2})")
                            await asyncio.sleep(random.uniform(0.6, 1.8))
                        except Exception as _pre_e2:
                            logger.debug(f"Pre-login ziyaret (2) atlandı: {_pre_e2}")

                    # Login öncesi son sayfa: doğrudan login URL'nin üst dizini
                    _parent_url = site["url"].rstrip("/").rsplit("/", 1)[0]
                    if _parent_url and _parent_url not in (_base_url, _country_url):
                        try:
                            logger.info(f"Pre-login ziyaret (3/3): {_parent_url}")
                            await page.goto(_parent_url, wait_until="domcontentloaded", timeout=15000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=6000)
                            except Exception:
                                pass
                            await handle_cf_interstitial()
                            await asyncio.sleep(random.uniform(0.8, 2.0))
                        except Exception as _pre_e3:
                            logger.debug(f"Pre-login ziyaret (3) atlandı: {_pre_e3}")

                logger.info(f"Login sayfasına gidiliyor: {site['url']}")
                failed_liftassets_urls.clear()
                await page.goto(site["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                await handle_cf_interstitial()

                # Vendor.js gibi liftassets kaynaklarının yüklenmesini bekle — insan gibi değişken süre
                await _sleep(random.uniform(6.0, 14.0), pct=0.0)

                # Login sayfası yüklendikten sonra küçük scroll — insan davranışı simüle et
                try:
                    _lp_scroll = random.randint(50, 200)
                    await page.evaluate(f"window.scrollBy(0, {_lp_scroll})")
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                    await page.evaluate(f"window.scrollBy(0, -{_lp_scroll})")
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                except Exception:
                    pass

                # Eğer liftassets istekleri başarısız olduysa, o URL'lere git ve CF cookie'si al
                if failed_liftassets_urls:
                    logger.info(f"Liftassets CF ısıtması başlıyor: {failed_liftassets_urls}")
                    for fa_url in failed_liftassets_urls[:3]:
                        await visit_and_solve_cf(fa_url)
                    # Login sayfasını tekrar yükle - şimdi liftassets CF cookie'si var
                    logger.info("Liftassets CF ısındı, login sayfası yeniden yükleniyor...")
                    failed_liftassets_urls.clear()
                    await page.goto(site["url"], wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await handle_cf_interstitial()
                    await _sleep(random.uniform(6.0, 14.0), pct=0.0)

                    if failed_liftassets_urls:
                        logger.warning("Liftassets retry sonrasında hala erişilemiyor - proxy uyumsuz")
                        return False

                return True

            # Session restore başarılıysa login akışını atla
            if _session_restored:
                logger.info("Login akışı atlanıyor (session geçerli)")
            else:
              liftassets_ok = await load_vfs_login_page()
              if not liftassets_ok:
                return {
                    "ok": False,
                    "status_label": "ERROR",
                    "matched_text": "liftassets_proxy_fail",
                    "signature": "proxy_liftassets_failed",
                }
            _login_already_done = _session_restored
            await _sleep(2)

            await click_first_visible(
                page,
                [
                    'button:has-text("Accept")',
                    'button:has-text("I Agree")',
                    'button:has-text("Allow All")',
                    'button:has-text("Agree")',
                    'button:has-text("Tüm Tanımlama Bilgilerini Kabul Et")',
                    'button:has-text("Tümünü Kabul Et")',
                    'button:has-text("Tümünü Reddet")',
                ],
            )
            await _sleep(1)

            email_selectors = [
                'input[formcontrolname="email"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[placeholder*="email" i]',
                'input[autocomplete="username"]'
            ]
            password_selectors = [
                'input[formcontrolname="password"]',
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="current-password"]'
            ]

            # Session restore → belki VFS dashboard'a yönlendirdi, email formu yok
            _skip_login_remaining = _login_already_done
            if not _skip_login_remaining:
                # Hızlı ön kontrol: zaten dashboard'da mıyız?
                _pre_body = (await page.locator("body").inner_text())[:300].lower()
                if any(x in _pre_body for x in ["start new booking", "sign out", "new application", "active application"]):
                    logger.info("✅ Cookies ile dashboard'a yönlendirildi — login adımları atlanıyor")
                    _skip_login_remaining = True

            if not _skip_login_remaining:
                email_selector = await wait_first_visible(page, email_selectors, timeout_ms=30000)
            else:
                email_selector = None
            if not email_selector and not _skip_login_remaining:
                html_preview = await page.content()
                with open(BASE_DIR / "debug_login_page.html", "w", encoding="utf-8") as f:
                    f.write(html_preview)

                body_preview = (await page.locator("body").inner_text())[:2000]
                logger.error(f"FORM YOK | URL={page.url}")
                logger.error(f"BODY PREVIEW:\n{body_preview}")

                await save_error_screenshot("email_bulunamadi")
                raise Exception("E-posta alanı bulunamadı")

            logger.info(f"Email alanı bulundu: {email_selector}")
            await fill_first_visible(page, email_selectors, _active_account["email"])
            await _sleep(1.2)

            ok_pwd = await fill_first_visible(page, password_selectors, _active_account["password"])
            if not ok_pwd and not _skip_login_remaining:
                await save_error_screenshot("password_bulunamadi")
                raise Exception("Şifre alanı bulunamadı")

            await _sleep(1.2)

            # Cloudflare Turnstile: otomatik geçmesini bekle, geçmezse tıkla
            logger.info("Cloudflare Turnstile kontrol ediliyor...")
            _cf_pre_wait = best_cf_wait_secs()  # optimizer'dan geçmiş başarı sürelerine göre
            logger.info(f"Turnstile ön bekleme: {_cf_pre_wait:.1f}s (optimizer)")
            await _sleep(_cf_pre_wait)
            _cf_start_ts = time.time()  # CF çözüm süresini ölçmeye başla
            _cf_elapsed[0] = 0.0  # her deneme başında sıfırla

            turnstile_passed = _skip_login_remaining  # dashboard'daysa Turnstile yok
            for attempt in range(0 if _skip_login_remaining else 20):
                cf_frame = next(
                    (f for f in page.frames if 'challenges.cloudflare.com' in f.url),
                    None
                )

                if not cf_frame:
                    logger.info(f"CF frame yok - Turnstile geçildi veya yüklenmedi ({attempt+1})")
                    turnstile_passed = True
                    break

                # CF frame var, checkbox durumunu kontrol et
                try:
                    # Turnstile checkbox checked mi?
                    cb = cf_frame.locator('input[type="checkbox"]')
                    if await cb.count() > 0:
                        checked = await cb.get_attribute("aria-checked")
                        if checked == "true":
                            logger.info("Turnstile zaten çözüldü (checkbox checked)!")
                            turnstile_passed = True
                            break
                except Exception:
                    pass

                # Sign In butonu aktif mi? (Turnstile geçildi göstergesi)
                try:
                    btn = page.locator('button:has-text("Sign In"), button:has-text("Sign in")').first
                    if await btn.is_visible():
                        disabled = await btn.get_attribute("disabled")
                        aria_disabled = await btn.get_attribute("aria-disabled")
                        if disabled is None and aria_disabled not in ("true", "True"):
                            logger.info("Sign In butonu aktif - Turnstile geçildi!")
                            turnstile_passed = True
                            break
                except Exception:
                    pass

                logger.info(f"CF frame bulundu (deneme {attempt+1}/20), tıklanıyor...")
                try:
                    fe = await cf_frame.frame_element()
                    bbox = await fe.bounding_box()
                    if bbox and bbox.get('width', 0) > 0:
                        click_x = bbox['x'] + min(22, bbox['width'] / 4) + random.uniform(-3, 3)
                        click_y = bbox['y'] + bbox['height'] / 2 + random.uniform(-2, 2)
                        logger.info(f"Turnstile tıklanıyor: ({click_x:.0f}, {click_y:.0f})")
                        await _human_move_to(page, click_x, click_y)
                        await asyncio.sleep(random.uniform(0.15, 0.4))
                        await page.mouse.click(click_x, click_y)
                        await _sleep(random.uniform(6.0, 11.0), pct=0.0)
                        continue
                except Exception as ex:
                    logger.debug(f"CF frame tıklama hatası: {ex}")

                await _sleep(4)
                logger.info(f"Turnstile bekleniyor ({attempt+1}/20)...")

            _cf_elapsed[0] = time.time() - _cf_start_ts
            logger.info(f"Turnstile {'geçildi' if turnstile_passed else 'geçilemedi'} — süre: {_cf_elapsed[0]:.1f}s")
            if not turnstile_passed:
                logger.warning("Turnstile 20 denemede çözülemedi, yine de devam ediliyor...")

            await _sleep(2)

            # Cookie popup hâlâ görünüyorsa kapat
            await click_first_visible(
                page,
                [
                    'button:has-text("Tüm Tanımlama Bilgilerini Kabul Et")',
                    'button:has-text("Tümünü Reddet")',
                    'button:has-text("Accept")',
                ],
            )
            await _sleep(1)

            signin_selectors = [
                'button:has-text("Sign In")',
                'button:has-text("Sign in")',
                'button:has-text("Login")',
                'button[type="submit"]'
            ]

            clicked = False
            for _retry in range(0 if _skip_login_remaining else 20):
                for sel in signin_selectors:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible():
                            disabled = await btn.get_attribute("disabled")
                            aria_disabled = await btn.get_attribute("aria-disabled")
                            if disabled is None and aria_disabled not in ("true", "True"):
                                logger.info(f"Sign in tıklanıyor: {sel}")
                                try:
                                    _bb = await btn.bounding_box()
                                    if _bb:
                                        await _human_move_to(page, _bb["x"] + _bb["width"] / 2, _bb["y"] + _bb["height"] / 2)
                                        await asyncio.sleep(random.uniform(0.1, 0.3))
                                except Exception:
                                    pass
                                await btn.click()
                                clicked = True
                                break
                    except Exception:
                        pass
                if clicked:
                    break
                # Son 5 denemede JS ile force-click dene (Angular disabled bazen kaldırılmaz)
                if _retry >= 15:
                    try:
                        result_js = await page.evaluate("""
                            (function(){
                                var btns = document.querySelectorAll('button[type="submit"], button');
                                for(var b of btns){
                                    var t = (b.textContent||'').toLowerCase();
                                    if(t.includes('sign in') || t.includes('login') || b.type==='submit'){
                                        b.removeAttribute('disabled');
                                        b.removeAttribute('aria-disabled');
                                        b.click();
                                        return b.textContent.trim();
                                    }
                                }
                                return null;
                            })()
                        """)
                        if result_js:
                            logger.info(f"Sign in JS force-click: {result_js!r}")
                            clicked = True
                            break
                    except Exception as e:
                        logger.warning(f"JS force-click hatası: {e}")
                logger.info(f"Sign In butonu henüz aktif değil ({_retry+1}/20)...")
                await _sleep(3)

            if not clicked and not _skip_login_remaining:
                await save_error_screenshot("signin_tiklanamadi")
                raise Exception("Sign in butonu tıklanamadı")

            await _sleep(random.uniform(3.0, 6.0), pct=0.0)
            logger.info(f"Sign in sonrası URL: {page.url}")

            body_preview = (await page.locator("body").inner_text())[:1000]
            logger.info(f"Sign-in sonrası body preview: {body_preview}")

            # Hesap seviyesi kısıtlama tespiti (Access Restricted for User ID)
            if any(x in body_preview.lower() for x in ["access restricted", "restricted access",
                                                         "unusual activity", "temporarily restricted"]):
                await save_error_screenshot("account_restricted")
                logger.error(f"⛔ Hesap kısıtlandı (Access Restricted): {body_preview[:200]}")
                return {
                    "ok": False,
                    "status_label": "ERROR",
                    "matched_text": "Hesap kısıtlandı — VFS support ile iletişime geçin",
                    "signature": "account_restricted",
                }

            # 429 / rate limit tespiti
            if any(x in body_preview.lower() or x in page.url.lower()
                   for x in ["429", "access denied", "unauthorised activity", "unauthorized activity",
                              "too many requests", "page-not-found"]):
                await save_error_screenshot("rate_limited")
                return {
                    "ok": False,
                    "status_label": "ERROR",
                    "matched_text": "VFS API rate limit (429) - lütfen bekleyin",
                    "signature": "rate_limited",
                }

            if any(x in body_preview.lower() for x in ["invalid", "incorrect", "wrong password", "unauthorized",
                                                         "not registered"]):
                await save_error_screenshot("login_reddedildi")
                raise Exception("Login bilgileri reddedildi")

            otp_selectors = [
                'input[formcontrolname="otp"]',
                'input[name="otp"]',
                'input[placeholder*="otp" i]',
                'input[placeholder*="verification" i]',
            ]

            # OTP beklemeden önce dashboard'da olup olmadığımızı kontrol et.
            # VFS bazen OTP atlar ve direkt dashboard'a gider; eğer öyleyse 30 saniyelik
            # beklemeyi atlıyoruz — bu sürede Angular arka planda API çağrıları yapıp
            # page-not-found'a yönlenebiliyor ve bot yanlışlıkla session expired sanıyor.
            if not _skip_login_remaining:
                _pre_otp_url = page.url
                _pre_otp_body = ""
                try:
                    _pre_otp_body = (await page.locator("body").inner_text())[:600]
                except Exception:
                    pass
                _dashboard_indicators_pre = ["sign out", "dashboard", "start new booking", "active application",
                                             "book appointment", "my applications", "no appointment"]
                if any(x in _pre_otp_body.lower() for x in _dashboard_indicators_pre) or (
                    "dashboard" in _pre_otp_url.lower() and "login" not in _pre_otp_url.lower()
                ):
                    logger.info(f"OTP bekleme atlanıyor — giriş öncesi dashboard tespit edildi. URL: {_pre_otp_url}")
                    _skip_login_remaining = True

            otp_selector = None if _skip_login_remaining else await wait_first_visible(page, otp_selectors, timeout_ms=30000)
            if not otp_selector and not _skip_login_remaining:
                # OTP gelmemiş olabilir — VFS bazen OTP atlar ve direkt dashboard'a gider
                _cur_url = page.url
                _body_check = (await page.locator("body").inner_text())[:600]
                _dashboard_indicators = ["sign out", "dashboard", "start new booking", "active application",
                                         "book appointment", "my applications", "no appointment"]
                _bad_urls = ("page-not-found", "session-expired", "error", "invalid", "401")
                _already_on_dashboard = (
                    any(x in _body_check.lower() for x in _dashboard_indicators)
                    or (
                        "login" not in _cur_url.lower()
                        and not any(x in _cur_url.lower() for x in _bad_urls)
                    )
                )
                if any(x in _cur_url.lower() for x in ("page-not-found", "session-expired")) or "session expired" in _body_check.lower():
                    logger.warning(f"Login sonrası kötü URL veya session expired — cookie siliniyor, retry: {_cur_url}")
                    _cpf.unlink(missing_ok=True)
                    raise Exception("VFS login sonrası session geçersiz (page-not-found/session-expired). Lütfen tekrar deneyin.")
                elif _already_on_dashboard:
                    logger.info(f"OTP adımı atlandı — VFS direkt dashboard'a geçti. URL: {_cur_url}")
                    # _skip_login_remaining True'ya çekerek OTP bloğunu atla
                    _skip_login_remaining = True
                else:
                    await save_error_screenshot("otp_input_yok")
                    # 30 sn beklendikten sonra sayfa içeriğini tekrar oku — Angular hata mesajı render olmuş olabilir
                    _body_recheck = (await page.locator("body").inner_text())[:1200].lower()
                    if "not registered" in _body_recheck:
                        raise Exception(
                            "VFS login reddedildi — bu e-posta VFS Global'de kayıtlı değil. "
                            "Lütfen doğru VFS hesap e-postasını girin."
                        )
                    if any(x in _body_recheck for x in ["invalid", "incorrect", "wrong password"]):
                        raise Exception("VFS login reddedildi — e-posta veya şifre hatalı.")
                    # Hâlâ login sayfasındayız — genel login hatası
                    raise Exception("VFS login başarısız — OTP aşamasına geçilemedi. Hesap bilgilerini kontrol edin.")

            if otp_selector:
                logger.info(f"OTP alanı bulundu: {otp_selector}")

            otp_code = None
            if otp_selector and wait_input_func:
                # Telegram üzerinden manuel OTP al
                email_display = _active_account["email"]
                if notify_func:
                    await notify_func(
                        f"📧 *{email_display}* hesabınıza bir OTP kodu gönderildi.\n"
                        f"Lütfen e-postanızı kontrol edip kodu aşağıya giriniz:"
                    )
                otp_code = await wait_input_func(
                    f"🔑 *{email_display}* adresine gelen *6 haneli OTP kodunu* girin:"
                )
                if not otp_code:
                    raise Exception("OTP girilmedi — zaman aşımı")
                otp_code = otp_code.strip()
            elif otp_selector and not _skip_login_remaining:
                # IMAP fallback (wait_input_func yoksa otomatik çek)
                otp_search_since = time.time() - 120
                for attempt in range(18):
                    logger.info(f"OTP deneme {attempt + 1}/18")
                    otp_code = await asyncio.to_thread(
                        get_vfs_otp_sync, _active_account["email"], _active_account["imap_password"], otp_search_since
                    )
                    if otp_code:
                        break
                    await _sleep(5)
                if not otp_code:
                    await save_error_screenshot("otp_mail_gelmedi")
                    raise Exception("OTP maili bulunamadı")

            logger.info(f"OTP bulundu: {otp_code}")
            await fill_first_visible(page, otp_selectors, otp_code)
            await _sleep(1)

            # OTP sayfasında da Turnstile olabilir - çöz
            logger.info("OTP sayfası Turnstile kontrol ediliyor...")
            otp_turnstile_passed = _skip_login_remaining
            for ts_attempt in range(0 if _skip_login_remaining else 20):
                cf_frame = next(
                    (f for f in page.frames if 'challenges.cloudflare.com' in f.url),
                    None
                )
                if not cf_frame:
                    logger.info("OTP sayfası Turnstile yok veya geçildi")
                    otp_turnstile_passed = True
                    break

                # Turnstile passed mı? Sign In aktif mi?
                try:
                    btn = page.locator('button:has-text("Sign In"), button:has-text("Sign in"), button[type="submit"]').first
                    if await btn.is_visible():
                        disabled = await btn.get_attribute("disabled")
                        aria_disabled = await btn.get_attribute("aria-disabled")
                        if disabled is None and aria_disabled not in ("true", "True"):
                            logger.info("OTP Turnstile geçildi (Sign In aktif)!")
                            otp_turnstile_passed = True
                            break
                except Exception:
                    pass

                logger.info(f"OTP Turnstile tıklanıyor (deneme {ts_attempt+1}/20)...")
                try:
                    fe = await cf_frame.frame_element()
                    bbox = await fe.bounding_box()
                    if bbox and bbox.get('width', 0) > 0:
                        click_x = bbox['x'] + min(22, bbox['width'] / 4)
                        click_y = bbox['y'] + bbox['height'] / 2
                        logger.info(f"OTP Turnstile tıklanıyor: ({click_x:.0f}, {click_y:.0f})")
                        await page.mouse.click(click_x, click_y)
                        await _sleep(random.uniform(6.0, 11.0), pct=0.0)
                        continue
                except Exception as ex:
                    logger.debug(f"OTP Turnstile tıklama hatası: {ex}")
                await _sleep(3)

            verify_selectors = [
                'button:has-text("Verify")',
                'button:has-text("Submit")',
                'button:has-text("Continue")',
                'button:has-text("Sign In")',
                'button:has-text("Sign in")',
                'button[type="submit"]'
            ]

            clicked_verify = False
            for _vretry in range(0 if _skip_login_remaining else 10):
                for sel in verify_selectors:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible():
                            disabled = await btn.get_attribute("disabled")
                            aria_disabled = await btn.get_attribute("aria-disabled")
                            if disabled is None and aria_disabled not in ("true", "True"):
                                logger.info(f"OTP verify tıklanıyor: {sel}")
                                await btn.click()
                                clicked_verify = True
                                break
                    except Exception:
                        pass
                if clicked_verify:
                    break
                logger.info(f"OTP verify butonu henüz aktif değil ({_vretry+1}/10)...")
                await _sleep(3)

            if not clicked_verify and not _skip_login_remaining:
                await page.keyboard.press("Enter")

            if _skip_login_remaining:
                logger.info("Session cookie ile login atlandı — dashboard kontrolü yapılıyor")
            else:
                logger.info("OTP doğrulandı, sayfa yüklenmesi bekleniyor...")

            # OTP doğrulama sonrası: URL'nin login'den ayrılmasını bekle (Angular router)
            try:
                await page.wait_for_url(
                    lambda url: "login" not in url,
                    timeout=30000,
                )
                logger.info(f"OTP sonrası URL değişti: {page.url}")
            except Exception:
                logger.info(f"URL login'den ayrılmadı (SPA normal olabilir): {page.url}")

            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await _sleep(5)

            await save_error_screenshot("otp_sonrasi_durum")

            logger.info(f"OTP sonrası URL: {page.url}")
            body_after_otp = (await page.locator("body").inner_text())[:500]
            logger.info(f"OTP sonrası body: {body_after_otp}")

            # 429 / hata sayfası tespiti
            error_keywords_post_otp = [
                "429", "access denied", "unauthorised activity", "unauthorized activity",
                "too many requests", "rate limit", "page-not-found", "blocked",
                "access restricted", "restricted access", "unusual activity", "temporarily restricted",
            ]
            if any(kw in body_after_otp.lower() or kw in page.url.lower() for kw in error_keywords_post_otp):
                await save_error_screenshot("otp_sonrasi_hata")
                return {
                    "ok": False,
                    "status_label": "ERROR",
                    "matched_text": f"OTP sonrası hata: {body_after_otp[:80]}",
                    "signature": "otp_error",
                }

            # Dashboard'a geçiş kontrolü — pozitif doğrulama
            dashboard_indicators = ["sign out", "dashboard", "start new booking", "active application"]
            on_dashboard = any(x in body_after_otp.lower() for x in dashboard_indicators)

            # Hala OTP/login sayfasındaysak
            otp_still_showing = any(x in body_after_otp.lower() for x in [
                "one time password", "please enter your otp", "otp", "sign in"
            ]) and not on_dashboard

            if otp_still_showing or not on_dashboard:
                # Bir kez daha bekle ve kontrol et (SPA geç render olabilir)
                await _sleep(5)
                body_recheck = (await page.locator("body").inner_text())[:500]
                on_dashboard = any(x in body_recheck.lower() for x in dashboard_indicators)
                if not on_dashboard:
                    logger.warning(f"OTP doğrulama başarısız — dashboard görünmüyor. Body: {body_recheck[:150]!r}")
                    await save_error_screenshot("otp_sonrasi_otp_sayfasi")
                    # Kullanıcıya bildir
                    if notify_func:
                        await notify_func("⚠️ OTP doğrulama başarısız (süresi dolmuş olabilir). Lütfen tekrar deneyin.")
                    return {
                        "ok": False,
                        "status_label": "ERROR",
                        "matched_text": "OTP doğrulama başarısız (dashboard açılmadı)",
                        "signature": "otp_verify_fail",
                    }

            # === SESSION COOKIES KAYDET / GÜNCELLE ===
            try:
                _ck = await page.context.cookies()
                if _ck:
                    _cpf.write_text(json.dumps(_ck), encoding="utf-8")
                    if _skip_login_remaining:
                        logger.info(f"Session cookies güncellendi (reuse): {_active_account['email']}")
                    else:
                        logger.info(f"✅ Session cookies kaydedildi ({len(_ck)} adet): {_active_account['email']}")
            except Exception as _cse:
                logger.warning(f"Cookie kayıt hatası: {_cse}")

            cfg = dynamic_config if dynamic_config else site.get("vfs_options", {})
            center_text = cfg.get("center_text", "") or (cfg.get("centers", [""])[0])
            category_text = cfg.get("category_text", "") or (cfg.get("categories", [""])[0])
            subcategory_text = cfg.get("subcategory_text", "") or (cfg.get("subcategories", [""])[0])
            logger.info(f"Seçimler: center={center_text!r}, category={category_text!r}, sub={subcategory_text!r}")

            # === MEVCUT BAŞVURULARI PARSE ET ===
            dashboard_body = await page.locator("body").inner_text()
            active_apps = []
            ref_matches = re.findall(r'Group Reference Number\s*[-–]\s*(\S+)', dashboard_body)
            sections = re.split(r'Group Reference Number\s*[-–]\s*\S+', dashboard_body)
            for i, ref in enumerate(ref_matches):
                chunk = sections[i + 1] if i + 1 < len(sections) else ""
                chunk_lower = chunk.lower()
                status = "Unknown"
                for s in ["payment processing", "confirmed", "cancelled", "pending", "submitted", "under review"]:
                    if s in chunk_lower:
                        status = s.title()
                        break
                name_m = re.search(r'Applicants?:\s*\n+([A-Z][A-Z ]+)', chunk)
                applicant = name_m.group(1).strip() if name_m else ""
                type_m = re.search(r'([A-Z /]+VISA|[A-Z /]+BUSINESS|[A-Z /]+TOURISM|[A-Z /]+SEJOUR)', chunk)
                app_type = type_m.group(1).strip() if type_m else ""
                active_apps.append({"ref": ref, "status": status, "applicant": applicant, "type": app_type})
                logger.info(f"Aktif başvuru bulundu: {ref} / {status} / {applicant}")

            # === GİRİŞ BAŞARILI — bildir ve önceden seçilen işlemi uygula ===
            if notify_func:
                await notify_func("✅ *Giriş başarılı!* İşleminiz başlatılıyor...")

            # Kullanıcının login öncesinde seçtiği işlem
            pre_action = (dynamic_config or {}).get("_action")
            if pre_action == "__sorgula__":
                resume_mode = True
                booking_mode = False
            elif pre_action == "__yeni__":
                booking_mode = True
                resume_mode = False

            if resume_mode:
                if not active_apps:
                    if notify_func:
                        await notify_func("📂 Aktif başvuru bulunamadı.")
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Aktif başvuru yok", "signature": "no_active_apps"}

                if wait_input_func:
                    rows = []
                    for app in active_apps:
                        label = f"📋 {app['ref']} — {app['status']}"
                        if app['applicant']:
                            label += f" ({app['applicant']})"
                        rows.append([InlineKeyboardButton(label[:60], callback_data=f"vfs_pick:{app['ref']}")])
                    rows.append([InlineKeyboardButton("➕ Yeni Başvuru Başlat", callback_data="vfs_pick:__new__")])

                    # Session canlı tutma: kullanıcı seçim yaparken arka planda 30s'de bir scroll
                    _session_keep_alive = True
                    async def _keep_session_alive():
                        while _session_keep_alive:
                            await _sleep(30)
                            if not _session_keep_alive:
                                break
                            try:
                                await page.evaluate("window.scrollBy(0, 1)")
                                await _sleep(0.5)
                                await page.evaluate("window.scrollBy(0, -1)")
                                logger.debug("Session keep-alive scroll yapıldı")
                            except Exception:
                                pass
                    _keep_alive_task = asyncio.create_task(_keep_session_alive())

                    picked = await wait_input_func("📂 *Mevcut Başvurularınız:*\nDevam etmek istediğiniz başvuruyu seçin:", rows)
                    _session_keep_alive = False
                    _keep_alive_task.cancel()

                    if picked == "__new__" or not picked:
                        pass  # Fall through to Start New Booking
                    else:
                        # Seçilen başvuruya git
                        logger.info(f"Başvuru seçildi: {picked}")

                        async def _navigate_to_app():
                            """Dashboard'daki başvuru linkine tıkla, sonra Angular yüklenene kadar bekle."""
                            clicked = False

                            # 0) Sayfa hala dashboard'da mı? Session expired olmuşsa login'e dön
                            current_body = await page.locator("body").inner_text()
                            if "session expired" in current_body.lower() or "session has expired" in current_body.lower():
                                logger.warning("Tıklamadan önce session expired tespit edildi")
                                return None

                            # Sayfanın hala yüklü olduğunu doğrula (dashboard refresh)
                            if "group reference" not in current_body.lower() and picked.lower() not in current_body.lower():
                                logger.info("Dashboard içeriği kaybolmuş, yenileniyor...")
                                try:
                                    await page.reload(wait_until="networkidle", timeout=20000)
                                    await _sleep(3)
                                except Exception:
                                    pass

                            # Dashboard'daki TÜM elementleri logla (href'siz a dahil)
                            try:
                                all_btns = await page.evaluate("""() => {
                                    return [...document.querySelectorAll('button, a')]
                                        .map(e => e.tagName + ':' + (e.textContent || '').trim().replace(/\\s+/g,' ').substring(0, 50) + ' href=' + (e.getAttribute('href') || e.getAttribute('routerlink') || ''))
                                        .filter(t => t.length > 5)
                                        .slice(0, 40);
                                }""")
                                logger.info(f"Dashboard tıklanabilir elementler: {all_btns}")
                            except Exception:
                                pass

                            # 1) "Appointment details" linkini doğrudan ara (Angular routerLink - href'siz)
                            try:
                                clicked_appt = await page.evaluate("""() => {
                                    const candidates = [...document.querySelectorAll('a, button')];
                                    for (const el of candidates) {
                                        const txt = (el.textContent || '').trim().toLowerCase();
                                        if (txt.includes('appointment') || txt.includes('application') || txt.includes('detail')) {
                                            el.click();
                                            return el.tagName + ':' + el.textContent.trim().substring(0, 60);
                                        }
                                    }
                                    return null;
                                }""")
                                if clicked_appt:
                                    logger.info(f"Appointment/detail linki tıklandı: {clicked_appt}")
                                    clicked = True
                            except Exception as e:
                                logger.debug(f"Appointment link hatası: {e}")

                            # 2) Ref numarasını içeren elementin ANCESTOR'ındaki buton/link (JavaScript ile)
                            if not clicked:
                                try:
                                    clicked_js = await page.evaluate(f"""() => {{
                                        // Ref numarasını içeren herhangi bir elementi bul (daha geniş arama)
                                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                        let node;
                                        while ((node = walker.nextNode())) {{
                                            if (node.textContent.includes('{picked}')) {{
                                                let ancestor = node.parentElement;
                                                for (let i = 0; i < 8; i++) {{
                                                    if (!ancestor || ancestor === document.body) break;
                                                    const tag = ancestor.tagName.toLowerCase();
                                                    // href'li veya href'siz a + button
                                                    if (tag === 'a' || tag === 'button') {{
                                                        ancestor.click();
                                                        return tag + ':' + ancestor.textContent.trim().substring(0, 50);
                                                    }}
                                                    // İçindeki ilk buton/link (href olmadan da)
                                                    const inner = ancestor.querySelector('button:not([disabled]), a');
                                                    if (inner) {{
                                                        inner.click();
                                                        return inner.tagName + ':' + (inner.textContent || '').trim().substring(0, 50);
                                                    }}
                                                    ancestor = ancestor.parentElement;
                                                }}
                                            }}
                                        }}
                                        return null;
                                    }}""")
                                    if clicked_js:
                                        logger.info(f"JS ile başvuru tıklandı: {clicked_js}")
                                        clicked = True
                                except Exception as e:
                                    logger.debug(f"JS navigate hatası: {e}")

                            # 2) Playwright locator ile dene
                            if not clicked:
                                for _nav_sel in [
                                    f'[href*="{picked}"]',
                                    f'a:has-text("{picked}")',
                                    f'button:has-text("{picked}")',
                                ]:
                                    try:
                                        el = page.locator(_nav_sel).first
                                        if await el.is_visible(timeout=2000):
                                            await el.click()
                                            logger.info(f"Başvuru linki tıklandı: {_nav_sel}")
                                            clicked = True
                                            break
                                    except Exception:
                                        pass

                            # 3) Son çare: URL ile git
                            if not clicked:
                                logger.warning("Dashboard'da tıklanabilir element bulunamadı, URL ile deneniyor")
                                base = site["url"].split("/book-appointment")[0] if "/book-appointment" in site["url"] else site["url"].rsplit("/", 1)[0]
                                detail_url = f"{base}/book-appointment/application-detail"
                                logger.info(f"URL ile navigate (son çare): {detail_url}")
                                try:
                                    await page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                                except Exception as e:
                                    logger.warning(f"Navigate hatası: {e}")

                            # 4) Angular içeriği yüklenene kadar poll et (60s)
                            for _wait_i in range(20):
                                await _sleep(3)
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=8000)
                                except Exception:
                                    pass
                                body_now = await page.locator("body").inner_text()
                                body_low = body_now.lower()
                                logger.info(f"Başvuru sayfası bekleniyor ({_wait_i+1}/20): {body_now[:120]!r}")
                                # Session expired / invalid → None döndür (hata olarak ele alınacak)
                                if "session expired" in body_low or "session has expired" in body_low or "invalid" in body_low and "session" in body_low:
                                    logger.warning("Session expired tespit edildi, uygulama detayı açılamadı")
                                    return None
                                # Anlamlı içerik geldi mi?
                                if any(kw in body_low for kw in [
                                    "reference", "applicant", "payment", "status", "appointment",
                                    "group reference", picked.lower(),
                                ]):
                                    logger.info("Başvuru sayfası yüklendi")
                                    return body_now
                            # Zaman aşımı — son hali döndür
                            return await page.locator("body").inner_text()

                        page_text = await _navigate_to_app()
                        if page_text is None:
                            # Cookie'yi sil — bir sonraki denemede fresh login yapılsın
                            _cpf.unlink(missing_ok=True)
                            if notify_func:
                                await notify_func("⚠️ VFS oturumu sona erdi. Lütfen tekrar deneyin.")
                            return {"ok": False, "status_label": "ERROR", "matched_text": "session_expired", "signature": "session_expired"}
                        logger.info(f"Başvuru sayfası (400): {page_text[:400]!r}")

                        # HTML source'da da ödeme URL'si ara (href içinde gizli olabilir)
                        html_source = await page.content()

                        def _find_pay_link(text):
                            for pattern in [
                                r'https?://[^\s"\'<>]+sanalpos[^\s"\'<>]+',
                                r'https?://[^\s"\'<>]+pay[^\s"\'<>]*\.teb[^\s"\'<>]+',
                                r'https?://[^\s"\'<>]+payment[^\s"\'<>]+',
                            ]:
                                m = re.search(pattern, text)
                                if m:
                                    return m.group(0)
                            return None

                        pay_link = _find_pay_link(page_text) or _find_pay_link(html_source)

                        # Ödeme butonu var mı? Tıkla ve sonra URL'yi yakala
                        if not pay_link:
                            for pay_sel in [
                                'a:has-text("Pay Online")',
                                'button:has-text("Pay Online")',
                                'a:has-text("Pay Now")',
                                'button:has-text("Pay")',
                                'a[href*="payment"]',
                                'a[href*="sanalpos"]',
                            ]:
                                try:
                                    btn = page.locator(pay_sel).first
                                    if await btn.is_visible(timeout=2000):
                                        href = await btn.get_attribute("href") or ""
                                        if href:
                                            pay_link = href
                                            logger.info(f"Ödeme href bulundu ({pay_sel}): {href}")
                                        else:
                                            # Tıkla ve yeni URL'yi yakala
                                            async with page.expect_navigation(timeout=10000):
                                                await btn.click()
                                            pay_link = page.url
                                            logger.info(f"Ödeme tıklandı, URL: {pay_link}")
                                        break
                                except Exception:
                                    pass

                        if not pay_link:
                            # HTML'deki tüm link href'lerini tara
                            all_hrefs = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                            for href in all_hrefs:
                                if "payment" in href.lower() or "sanalpos" in href.lower() or "pay" in href.lower():
                                    pay_link = href
                                    logger.info(f"Link taramasında ödeme URL'si: {pay_link}")
                                    break

                        if pay_link:
                            app_status = next((a['status'] for a in active_apps if a['ref'] == picked), '')
                            await notify_func(f"💳 *Ödeme sayfası bulundu!*\nBaşvuru: `{picked}` — {app_status}")
                            return await _proceed_payment(pay_link)

                        # Ödeme linki bulunamadı — sayfayı bildir
                        if notify_func:
                            await notify_func(f"📋 *Başvuru Durumu:*\n\n{page_text[:500]}")
                        return {
                            "ok": True,
                            "status_label": "PENDING",
                            "matched_text": f"Başvuru: {picked}\n{page_text[:200]}",
                            "signature": hashlib.sha256(f"resume:{picked}".encode()).hexdigest()[:16],
                        }

            # === MEVCUT BAŞVURULAR (booking_mode'da da göster) ===
            if booking_mode and active_apps and wait_input_func:
                rows = []
                for app in active_apps:
                    label = f"📋 {app['ref']} — {app['status']}"
                    if app['applicant']:
                        label += f" ({app['applicant']})"
                    rows.append([InlineKeyboardButton(label[:60], callback_data=f"vfs_pick:{app['ref']}")])
                rows.append([InlineKeyboardButton("➕ Yeni Başvuru", callback_data="vfs_pick:__new__")])
                picked = await wait_input_func(
                    "📂 *Dashboard'da mevcut başvurular var:*\nMevcut birine devam et veya yeni başvuru başlat:",
                    rows
                )
                if picked and picked != "__new__":
                    # Mevcut başvuruya resume akışına yönlendir — aynı _navigate_to_app mantığı
                    logger.info(f"Booking modunda mevcut başvuru seçildi: {picked}")
                    clicked2 = False
                    for _nav_sel2 in [f'[href*="{picked}"]', f'a:has-text("{picked}")', f'button:has-text("{picked}")', 'a:has-text("View")', 'button:has-text("Continue")']:
                        try:
                            el2 = page.locator(_nav_sel2).first
                            if await el2.is_visible(timeout=2000):
                                await el2.click()
                                clicked2 = True
                                break
                        except Exception:
                            pass
                    if not clicked2:
                        base2 = site["url"].split("/book-appointment")[0] if "/book-appointment" in site["url"] else site["url"].rsplit("/", 1)[0]
                        try:
                            await page.goto(f"{base2}/book-appointment/application-detail", wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                    # Angular yüklenene kadar poll et
                    page_text2 = ""
                    for _wi2 in range(20):
                        await _sleep(3)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        page_text2 = await page.locator("body").inner_text()
                        if any(kw in page_text2.lower() for kw in ["reference", "applicant", "payment", "status", picked.lower()]):
                            break
                        logger.info(f"Booking resume bekleniyor ({_wi2+1}/20): {page_text2[:80]!r}")
                    html2 = await page.content()
                    pay_link2 = None
                    for _pat2 in [r'https?://[^\s"\'<>]+sanalpos[^\s"\'<>]+', r'https?://[^\s"\'<>]+pay[^\s"\'<>]*\.teb[^\s"\'<>]+']:
                        m2 = re.search(_pat2, page_text2) or re.search(_pat2, html2)
                        if m2:
                            pay_link2 = m2.group(0)
                            break
                    if not pay_link2:
                        hrefs2 = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                        for h2 in hrefs2:
                            if "payment" in h2.lower() or "sanalpos" in h2.lower():
                                pay_link2 = h2
                                break
                    if pay_link2:
                        await notify_func(f"💳 *Ödeme sayfası bulundu!*\nBaşvuru: `{picked}`")
                        return await _proceed_payment(pay_link2)
                    if notify_func:
                        await notify_func(f"📋 Başvuru sayfası:\n{page_text2[:400]}")
                    return {"ok": True, "status_label": "PENDING", "matched_text": page_text2[:200],
                            "signature": hashlib.sha256(f"resume:{picked}".encode()).hexdigest()[:16]}
                # __new__ veya seçim yok → Start New Booking'e devam

            # "Start New Booking" butonu bekle ve tıkla
            # Angular Material button'u bulmak için Playwright'ın role+text locator'larını kullan
            booking_clicked = False
            for _btn_attempt in range(24):  # 24 × 3s = 72s
                try:
                    # Önce role-based locator (en güvenilir)
                    btn = page.get_by_role("button", name="Start New Booking")
                    cnt = await btn.count()
                    if cnt > 0:
                        logger.info(f"get_by_role 'Start New Booking' bulundu ({cnt} adet), tıklanıyor...")
                        await btn.first.click()
                        booking_clicked = True
                        break
                except Exception as _e:
                    logger.debug(f"get_by_role Start New Booking hata: {_e}")

                if not booking_clicked:
                    # Alternatif: body text içinde "Start New Booking" geçiyorsa doğrudan tıkla
                    try:
                        btn2 = page.locator("button").filter(has_text="Start New Booking")
                        cnt2 = await btn2.count()
                        if cnt2 > 0:
                            logger.info(f"filter(has_text) 'Start New Booking' bulundu, tıklanıyor...")
                            await btn2.first.click()
                            booking_clicked = True
                            break
                    except Exception as _e2:
                        logger.debug(f"filter has_text hata: {_e2}")

                logger.info(f"Booking butonu bekleniyor ({_btn_attempt+1}/24)...")
                await _sleep(3)

            if not booking_clicked:
                await save_error_screenshot("booking_btn_yok")
                page_text = (await page.locator("body").inner_text())[:500]
                logger.warning(f"'Start New Booking' butonu bulunamadı. Sayfa: {page_text}")
                raise Exception("'Start New Booking' butonu bulunamadı")

            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await _sleep(5)

            logger.info(f"Booking sonrası URL: {page.url}")
            await save_error_screenshot("booking_btn_sonrasi")

            # mat-select için bekleme - yavaş Angular yüklenmesini tolere et
            center_sel = 'mat-select[formcontrolname="centerCode"]'
            center_visible = await wait_first_visible(page, [center_sel], timeout_ms=60000)
            if not center_visible:
                await save_error_screenshot("center_dropdown_yok")
                page_text = (await page.locator("body").inner_text())[:1000]
                logger.error(f"centerCode dropdown yok. Sayfa: {page_text}")
                raise Exception("centerCode dropdown bulunamadı")

            async def _proceed_booking():
                """VFS booking akışı: Kişisel bilgiler (Your Details) → Takvim (Book Appointment) → Submit.
                VFS akış sırası: 1.Appointment Details → 2.Your Details → 3.Book Appointment → 4.Services → 5.Review
                Bu fonksiyon 2.Your Details aşamasından başlar.
                """
                logger.info("=== BOOKING AKIŞI BAŞLADI (Your Details → Book Appointment) ===")

                # Mevcut sayfanın durumunu kontrol et (timeout ile — askı önlemi)
                _pb_url = page.url
                try:
                    _pb_body = await asyncio.wait_for(
                        page.locator("body").inner_text(), timeout=12.0
                    )
                except asyncio.TimeoutError:
                    logger.error("_proceed_booking: inner_text() 12s içinde gelmedi — iptal")
                    if notify_func:
                        await notify_func(
                            "⚠️ *Sayfa yanıt vermedi* (12s timeout).\n"
                            "VFS sayfası yüklenemedi — lütfen birkaç saniye bekleyip tekrar deneyin."
                        )
                    return {"ok": False, "status_label": "ERROR",
                            "matched_text": "Sayfa zaman aşımı", "signature": "page_timeout"}
                except Exception as _pbe:
                    logger.error(f"_proceed_booking: inner_text() hatası: {_pbe}")
                    if notify_func:
                        await notify_func(f"⚠️ Sayfa okuma hatası: {str(_pbe)[:100]}")
                    return {"ok": False, "status_label": "ERROR",
                            "matched_text": str(_pbe)[:80], "signature": "page_error"}
                _pb_lower = _pb_body.lower()
                logger.info(f"_proceed_booking başlangıç: URL={_pb_url}, sayfa (200): {_pb_body[:200]!r}")

                # Eğer hâlâ appointment-details sayfasındaysak (API fallback yolundan geliyoruz)
                # Continue'yu force-click ile geç
                # NOT: "appointment details" stepper başlığında da görünür (your-details sayfasında bile!)
                # Sadece "choose your application centre" gerçekten appointment-detail sayfasına özgüdür
                _on_appt_details = (
                    "choose your application centre" in _pb_lower or
                    "choose your centre" in _pb_lower
                ) and "appointment-detail" in _pb_url.lower()
                if _on_appt_details:
                    logger.info("Hâlâ appointment-details sayfasında — Continue force-click deneniyor...")
                    for _fa in range(5):
                        try:
                            _fr = await page.evaluate("""() => {
                                const btns = Array.from(document.querySelectorAll('button'));
                                for (const b of btns) {
                                    const t = b.textContent.trim().toLowerCase();
                                    if (t === 'continue' || t === 'save' || b.type === 'submit') {
                                        b.removeAttribute('disabled');
                                        b.removeAttribute('aria-disabled');
                                        b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                        return 'clicked:' + b.textContent.trim();
                                    }
                                }
                                return 'not-found';
                            }""")
                            logger.info(f"Force Continue deneme {_fa+1}: {_fr}")
                            if _fr and _fr != 'not-found':
                                try:
                                    await page.wait_for_load_state("networkidle", timeout=12000)
                                except Exception:
                                    pass
                                await _sleep(3)
                                break
                        except Exception as _fe2:
                            logger.warning(f"Force Continue hata: {_fe2}")
                        await _sleep(2)

                # Ekran görüntüsü al — mevcut sayfa durumunu kaydet
                _ss_now = f"/tmp/vfs_proceed_booking_{int(time.time())}.png"
                try:
                    await page.screenshot(path=_ss_now, full_page=True)
                    logger.info(f"_proceed_booking ekran görüntüsü: {_ss_now}")
                    await send_admin_photo(_ss_now, f"📸 Randevu sayfası: {_ss_now}")
                except Exception:
                    pass

                # === KİŞİSEL BİLGİLER (Your Details — Aşama 2) ===

                async def _yd_fill(loc, value: str) -> bool:
                    """Verilen locator'a değer yazar."""
                    try:
                        if await loc.is_visible(timeout=5000):
                            await loc.click()
                            await loc.fill("")
                            await loc.type(str(value), delay=random.randint(20, 60))
                            return True
                    except Exception as e:
                        logger.debug(f"_yd_fill: {e}")
                    return False

                def _yd_css(text: str) -> str:
                    """Placeholder metnini içeren input için CSS selector üretir."""
                    safe = text.replace('"', '')
                    return f'input[placeholder*="{safe}" i], textarea[placeholder*="{safe}" i]'

                async def _yd_has(placeholder: str) -> bool:
                    """Sayfada bu placeholder veya label'a sahip görünür bir input var mı?"""
                    try:
                        if await page.locator(_yd_css(placeholder)).first.is_visible(timeout=3000):
                            return True
                    except Exception:
                        pass
                    try:
                        if await page.get_by_label(placeholder, exact=False).first.is_visible(timeout=1000):
                            return True
                    except Exception:
                        pass
                    # JS fallback: Angular overlay sebebiyle Playwright is_visible False dönebilir
                    try:
                        ph_lower = placeholder.lower()
                        found = await page.evaluate(
                            """(ph) => Array.from(document.querySelectorAll("input, textarea"))
                               .some(el => el.offsetHeight > 0 && (el.placeholder || "").toLowerCase().includes(ph))""",
                            ph_lower
                        )
                        if found:
                            return True
                    except Exception:
                        pass
                    return False

                async def _yd_write(placeholder: str, value: str, label: str) -> bool:
                    """CSS selector veya label ile input'u bulup değer yazar."""
                    loc = page.locator(_yd_css(placeholder)).first
                    ok = await _yd_fill(loc, value)
                    if not ok:
                        loc2 = page.get_by_label(placeholder, exact=False).first
                        ok = await _yd_fill(loc2, value)
                    if not ok:
                        # JS fallback: Angular overlay varsa doğrudan DOM üzerinden doldur
                        try:
                            ph_lower = placeholder.lower()
                            ok = await page.evaluate(
                                """([ph, val]) => {
                                    const el = Array.from(document.querySelectorAll("input, textarea"))
                                        .find(e => e.offsetHeight > 0 && (e.placeholder || "").toLowerCase().includes(ph));
                                    if (!el) return false;
                                    el.focus();
                                    const nativeInput = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                                    if (nativeInput) nativeInput.set.call(el, val);
                                    else el.value = val;
                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }""",
                                [ph_lower, str(value)]
                            )
                            if ok:
                                logger.info(f"Dolduruldu JS ile ({label}): {value}")
                        except Exception as _jfe:
                            logger.debug(f"_yd_write JS fallback: {_jfe}")
                    if ok:
                        logger.info(f"Dolduruldu ({label}): {value}")
                    else:
                        logger.warning(f"Alan bulunamadı: {label} (placeholder={placeholder!r})")
                        await save_error_screenshot(f"alan_yok_{label}")
                    return ok

                # VFS "Please wait X seconds" uyarısını tespit et ve bekle
                _yd_wait_secs = 0
                try:
                    _yd_body_raw = await page.locator("body").inner_text()
                    import re as _re
                    _yd_m = _re.search(r"wait\s+(\d+)\s+seconds?", _yd_body_raw, _re.IGNORECASE)
                    if _yd_m:
                        _yd_wait_secs = int(_yd_m.group(1)) + 2
                except Exception:
                    pass

                # Form alanlarının Angular tarafından render edilmesini bekle (max 60s)
                _form_ready = False
                for _fr in range(60):
                    if await _yd_has("ENTER YOUR FIRST NAME") or await _yd_has("FIRST NAME") or await _yd_has("First Name"):
                        _form_ready = True
                        break
                    # JS ile direkt DOM kontrolü (Playwright is_visible bazen Angular overlay'den False döner)
                    try:
                        _js_ready = await page.evaluate(
                            """() => Array.from(document.querySelectorAll("input"))
                               .some(el => el.offsetHeight > 0 && (el.placeholder || "").toLowerCase().includes("first"))"""
                        )
                        if _js_ready:
                            _form_ready = True
                            logger.info("Form JS ile hazır tespit edildi")
                            break
                    except Exception:
                        pass
                    _fr_body = (await page.locator("body").inner_text())[:300].lower()
                    if "session expired" in _fr_body or "401" in page.url:
                        logger.warning("Kişisel bilgiler formu beklenirken session sona erdi")
                        _cpf.unlink(missing_ok=True)
                        if notify_func:
                            await notify_func("⚠️ VFS oturumu form aşamasında sona erdi. Lütfen tekrar deneyin.")
                        return {"ok": False, "status_label": "ERROR", "matched_text": "session_expired", "signature": "session_expired"}
                    await _sleep(1)
                if not _form_ready:
                    logger.warning("Kişisel bilgiler formu 60s içinde yüklenmedi — 5s daha bekleniyor")
                    await _sleep(5)
                await save_error_screenshot("your_details_form_baslangic")

                # DOM debug: sayfadaki tüm input'ların placeholder/formcontrolname değerlerini logla
                try:
                    _inp_dbg = await page.evaluate("""() =>
                        Array.from(document.querySelectorAll("input, textarea")).map(el => ({
                            ph: el.placeholder, fcn: el.getAttribute("formcontrolname"),
                            type: el.type, name: el.name, visible: el.offsetHeight > 0
                        }))
                    """)
                    logger.info(f"DOM input debug: {_inp_dbg}")
                except Exception as _de:
                    logger.debug(f"DOM debug err: {_de}")

                # Sayfadaki alanları dinamik olarak tespit et
                _has_firstname    = await _yd_has("ENTER YOUR FIRST NAME") or await _yd_has("First Name")
                _has_lastname     = await _yd_has("PLEASE ENTER LAST NAME") or await _yd_has("ENTER LAST NAME") or await _yd_has("Last Name")
                _has_passport     = await _yd_has("ENTER PASSPORT NUMBER") or await _yd_has("Passport Number")
                _has_dob          = await _yd_has("Date of Birth") or await _yd_has("DD/MM/YYYY") or await _yd_has("dateOfBirth")
                _has_gender       = False
                try:
                    _has_gender = await page.locator('mat-select[formcontrolname="gender"]').is_visible(timeout=1000)
                except Exception:
                    pass
                _has_nationality  = False
                try:
                    _has_nationality = await page.locator('mat-select[formcontrolname="nationality"], mat-select[formcontrolname="nationalityId"]').first.is_visible(timeout=1000)
                except Exception:
                    pass
                _has_phone        = await _yd_has("Contact number") or await _yd_has("Phone Number") or await _yd_has("Contact Number") or await _yd_has("012345648382")
                _has_passport_exp = await _yd_has("Expiry") or await _yd_has("EXPIRY") or await _yd_has("passportExpiryDate")
                _has_email        = await _yd_has("Email") or await _yd_has("Email Address") or await _yd_has("ENTER EMAIL ADDRESS")

                logger.info(f"YD alanları: firstname={_has_firstname} lastname={_has_lastname} passport={_has_passport} "
                            f"dob={_has_dob} gender={_has_gender} nationality={_has_nationality} "
                            f"phone={_has_phone} passport_exp={_has_passport_exp} email={_has_email}")

                # Hiçbir alan bulunamadıysa JS ile DOM'dan placeholder listesi alıp override et
                if not any([_has_firstname, _has_lastname, _has_passport, _has_phone, _has_email]):
                    try:
                        _dom_phs = await page.evaluate(
                            """() => Array.from(document.querySelectorAll("input, textarea"))
                               .filter(el => el.offsetHeight > 0)
                               .map(el => (el.placeholder || "").toLowerCase())"""
                        )
                        logger.info(f"JS DOM placeholder listesi: {_dom_phs}")
                        def _jph(kw): return any(kw in ph for ph in _dom_phs)
                        if _jph("first"):    _has_firstname = True
                        if _jph("last"):     _has_lastname  = True
                        if _jph("passport"): _has_passport  = True
                        if _jph("birth") or _jph("dd/mm"): _has_dob = True
                        if _jph("contact") or _jph("phone") or _jph("012345"): _has_phone = True
                        if _jph("expir"):    _has_passport_exp = True
                        if _jph("email"):    _has_email = True
                        logger.info(f"JS override sonrası: firstname={_has_firstname} lastname={_has_lastname} "
                                    f"passport={_has_passport} dob={_has_dob} phone={_has_phone} email={_has_email}")
                    except Exception as _jde:
                        logger.debug(f"JS alan override hatası: {_jde}")

                # Müşteri kaydından otomatik doldur (varsa), yoksa Telegram'dan sor
                _cust = dynamic_config.get("customer")
                if _cust:
                    _cname_parts = (_cust.name or "").strip().split(None, 1)
                    _cust_first = _cname_parts[0] if _cname_parts else ""
                    _cust_last  = _cname_parts[1] if len(_cname_parts) > 1 else ""
                    logger.info(f"Müşteri auto-fill: {_cust.name}, pasaport={_cust.passport_no}, tel={_cust.phone}, email={_cust.email}")
                    if notify_func:
                        await notify_func(f"📝 Kişisel bilgiler *{_cust.name}* için otomatik dolduruluyor...")
                else:
                    _cust_first = _cust_last = ""

                if not wait_input_func and not _cust:
                    pass
                else:
                    if _has_firstname:
                        val = _cust_first or (await wait_input_func("👤 *Ad (First Name)* girin:") if wait_input_func else "")
                        if val:
                            await _yd_write("First Name", val, "firstName")

                    if _has_lastname:
                        val = _cust_last or (await wait_input_func("👤 *Soyad (Last Name)* girin:") if wait_input_func else "")
                        if val:
                            await _yd_write("Last Name", val, "lastName")

                    if _has_dob:
                        val = await wait_input_func("📅 *Doğum Tarihi* girin (DD/MM/YYYY — örn: `01/01/1990`):") if wait_input_func else ""
                        if val:
                            await _yd_write("Date of Birth", val, "dateOfBirth")

                    if _has_gender:
                        gender_btns = [[
                            InlineKeyboardButton("👨 Erkek (Male)",   callback_data="vfs_pick:Male"),
                            InlineKeyboardButton("👩 Kadın (Female)", callback_data="vfs_pick:Female"),
                        ]]
                        val = (await wait_input_func("⚧ *Cinsiyet (Gender) seçin:*", gender_btns)) if wait_input_func else ""
                        if val:
                            await select_mat_option('mat-select[formcontrolname="gender"]', val, "Cinsiyet")

                    if _has_nationality:
                        _cust_natl = (_cust.nationality if _cust else "") or ""
                        if _cust_natl:
                            val = _cust_natl
                        else:
                            _mc = (site.get("mission_code") or "").upper()
                            _NATL_MAP = {
                                "DNK": ("🇩🇰 Danimarkalı", "Danish"),
                                "FRA": ("🇫🇷 Fransız",     "French"),
                                "NLD": ("🇳🇱 Hollandalı",  "Dutch"),
                                "SWE": ("🇸🇪 İsveçli",     "Swedish"),
                                "AUT": ("🇦🇹 Avusturyalı", "Austrian"),
                                "BEL": ("🇧🇪 Belçikalı",   "Belgian"),
                                "DEU": ("🇩🇪 Alman",        "German"),
                                "CHE": ("🇨🇭 İsviçreli",   "Swiss"),
                                "ITA": ("🇮🇹 İtalyan",     "Italian"),
                                "ESP": ("🇪🇸 İspanyol",    "Spanish"),
                                "GRC": ("🇬🇷 Yunan",       "Greek"),
                            }
                            _dest_code = next((k for k in _NATL_MAP if k in _mc), None)
                            if _dest_code:
                                _dest_label, _dest_val = _NATL_MAP[_dest_code]
                                _natl_btns = [[
                                    InlineKeyboardButton("🇹🇷 Türk",  callback_data="vfs_pick:Turkish"),
                                    InlineKeyboardButton(_dest_label, callback_data=f"vfs_pick:{_dest_val}"),
                                ]]
                                val = (await wait_input_func("🌍 *Vatandaşlık (Nationality) seçin:*", _natl_btns)) if wait_input_func else ""
                            else:
                                val = (await wait_input_func("🌍 *Vatandaşlık* girin (örn: Turkish):")) if wait_input_func else ""
                        if val:
                            await select_mat_option(
                                'mat-select[formcontrolname="nationality"]', val, "Vatandaşlık"
                            ) or await select_mat_option(
                                'mat-select[formcontrolname="nationalityId"]', val, "Vatandaşlık2"
                            )

                    if _has_passport:
                        val = (_cust.passport_no if _cust else "") or (await wait_input_func("🛂 *Pasaport Numarası* girin:") if wait_input_func else "")
                        if val:
                            await _yd_write("Passport Number", val, "passportNumber")

                    if _has_passport_exp:
                        val = (await wait_input_func("📅 *Pasaport Son Geçerlilik Tarihi* girin (DD/MM/YYYY):")) if wait_input_func else ""
                        if val:
                            await _yd_write("Expiry", val, "passportExpiry")

                    if _has_phone:
                        _cust_phone = (_cust.phone if _cust else "") or ""
                        if _cust_phone:
                            # Telefon numarasını ülke kodu + numara olarak ayır
                            _ph = _cust_phone.lstrip("+").strip()
                            if _ph.startswith("90") and len(_ph) > 10:
                                val_code, val_num = "90", _ph[2:]
                            elif _ph.startswith("0"):
                                val_code, val_num = "90", _ph[1:]
                            else:
                                val_code, val_num = "", _ph
                        else:
                            val_code = (await wait_input_func("📞 *Telefon Ülke Kodu* girin (sadece rakam, örn: `90` Türkiye, `44` UK):")) if wait_input_func else ""
                            val_num  = (await wait_input_func("📞 *Telefon Numarası* girin (başında 0 olmadan):")) if wait_input_func else ""
                        if val_code:
                            try:
                                _code_inputs = page.locator('input[formcontrolname="countryCode"], input[maxlength="4"]')
                                if await _code_inputs.first.is_visible(timeout=2000):
                                    await _yd_fill(_code_inputs.first, val_code)
                                    logger.info(f"Ülke kodu dolduruldu: {val_code}")
                            except Exception as _ce:
                                logger.debug(f"Ülke kodu fill: {_ce}")
                        if val_num:
                            await _yd_write("Contact number", val_num, "contactNumber")

                    if _has_email:
                        val = (_cust.email if _cust else "") or (await wait_input_func("📧 *E-posta* girin:") if wait_input_func else "")
                        if val:
                            await _yd_write("Email", val, "email")

                # VFS sayım bekleme süresi dolmadıysa bekle (Save düğmesine tıklamadan önce)
                if _yd_wait_secs > 0:
                    logger.info(f"VFS form bekleme süresi: {_yd_wait_secs}s bekleniyor...")
                    await asyncio.sleep(_yd_wait_secs)

                # Your Details sonrası ekran görüntüsü al
                _ss_yd = f"/tmp/vfs_your_details_filled_{int(time.time())}.png"
                try:
                    await page.screenshot(path=_ss_yd, full_page=True)
                    logger.info(f"Your Details dolduruldu ekran görüntüsü: {_ss_yd}")
                    await send_admin_photo(_ss_yd, f"📸 Kişisel bilgiler dolduruldu: {_ss_yd}")
                except Exception:
                    pass

                # === YOUR DETAILS → CONTINUE → BOOK APPOINTMENT (Takvim/Calendar) ===
                # Kişisel bilgiler doldurulduktan sonra Continue'ya tıkla → Takvim sayfasına geç
                _yd_cont_clicked = False
                for _yda in range(12):
                    try:
                        _ydb = page.locator('button:has-text("Continue"), button:has-text("Save"), button[type="submit"]').first
                        if await _ydb.is_visible(timeout=2000):
                            _ydb_dis = await _ydb.get_attribute("disabled")
                            _ydb_aria = await _ydb.get_attribute("aria-disabled")
                            if _ydb_dis is None and _ydb_aria not in ("true", "True"):
                                await _ydb.click()
                                logger.info("Your Details Continue tıklandı")
                                _yd_cont_clicked = True
                                break
                    except Exception:
                        pass
                    await _sleep(2)

                if _yd_cont_clicked:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await _sleep(3)

                    # Book Appointment (Takvim) sayfasında mıyız?
                    _ba_url = page.url
                    _ba_body = await page.locator("body").inner_text()
                    _ba_lower = _ba_body.lower()
                    logger.info(f"Your Details Continue sonrası URL: {_ba_url}, sayfa (200): {_ba_body[:200]!r}")

                    # Ekran görüntüsü al
                    _ss_ba = f"/tmp/vfs_book_appointment_{int(time.time())}.png"
                    try:
                        await page.screenshot(path=_ss_ba, full_page=True)
                        logger.info(f"Book Appointment ekran görüntüsü: {_ss_ba}")
                        await send_admin_photo(_ss_ba, f"📸 Takvim/Randevu tarihi seçim sayfası: {_ss_ba}")
                    except Exception:
                        pass

                    # === TARİH SEÇİMİ (Book Appointment — Aşama 3) ===
                    _on_calendar = any(x in _ba_lower for x in [
                        "book appointment", "select date", "calendar", "available date",
                        "randevu tarihi", "tarih seç", "takvim"
                    ])
                    if _on_calendar or True:  # Her durumda tarih seçimi dene
                        if notify_func:
                            await notify_func("📅 *Randevu tarihi seçin:*")
                        available_dates = []
                        try:
                            date_els = await page.locator(
                                'mat-calendar-body-cell:not([aria-disabled="true"]), '
                                '[class*="slot-date"]:not([disabled]), '
                                'button[aria-label]:not([aria-disabled="true"])'
                            ).all()
                            for _el in date_els:
                                try:
                                    lbl = (await _el.get_attribute("aria-label") or await _el.inner_text()).strip()
                                    if lbl and any(c.isdigit() for c in lbl) and len(lbl) < 50:
                                        available_dates.append(lbl)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        if not available_dates:
                            _date_hits = re.findall(r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b', _ba_body)
                            available_dates = list(dict.fromkeys(_date_hits))[:12]

                        if available_dates and wait_input_func:
                            _d_rows, _d_row = [], []
                            for _d in available_dates[:12]:
                                _d_row.append(InlineKeyboardButton(_d, callback_data=f"vfs_pick:{_d}"))
                                if len(_d_row) == 2:
                                    _d_rows.append(_d_row); _d_row = []
                            if _d_row:
                                _d_rows.append(_d_row)
                            date_val = await wait_input_func("📅 *Randevu Tarihi Seçin:*", _d_rows)
                            if date_val:
                                logger.info(f"Kullanıcı tarih seçti: {date_val}")
                                try:
                                    _del = page.locator(f'[aria-label*="{date_val}"]').first
                                    if not await _del.is_visible(timeout=2000):
                                        _del = page.locator(f'button:has-text("{date_val}"), td:has-text("{date_val}")').first
                                    await _del.click()
                                    logger.info(f"Tarih tıklandı: {date_val}")
                                except Exception as _de:
                                    logger.warning(f"Tarih tıklama hatası: {_de}")
                                await _sleep(2)

                                # Saat seçimi
                                await _sleep(2)
                                _time_body = await page.locator("body").inner_text()
                                available_times = []
                                try:
                                    _time_els = await page.locator(
                                        '[class*="slot"], [class*="time-slot"], mat-list-item, [class*="timeslot"]'
                                    ).all()
                                    for _tel in _time_els:
                                        try:
                                            _ttxt = (await _tel.inner_text()).strip()
                                            if re.match(r'\d{1,2}:\d{2}', _ttxt):
                                                available_times.append(_ttxt)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                if not available_times:
                                    _t_hits = re.findall(r'\b(\d{1,2}:\d{2}(?::\d{2})?)\b', _time_body)
                                    available_times = list(dict.fromkeys(_t_hits))

                                if available_times and wait_input_func:
                                    _t_rows, _t_row = [], []
                                    for _t in available_times[:12]:
                                        _t_row.append(InlineKeyboardButton(_t, callback_data=f"vfs_pick:{_t}"))
                                        if len(_t_row) == 3:
                                            _t_rows.append(_t_row); _t_row = []
                                    if _t_row:
                                        _t_rows.append(_t_row)
                                    time_val = await wait_input_func("⏰ *Randevu Saatini Seçin:*", _t_rows)
                                    if time_val:
                                        logger.info(f"Kullanıcı saat seçti: {time_val}")
                                        try:
                                            _sel_t = page.locator(
                                                f'[class*="slot"]:has-text("{time_val}"), '
                                                f'mat-list-item:has-text("{time_val}"), '
                                                f'button:has-text("{time_val}")'
                                            ).first
                                            if await _sel_t.is_visible(timeout=2000):
                                                await _sel_t.click()
                                                logger.info(f"Saat tıklandı: {time_val}")
                                        except Exception as _te:
                                            logger.warning(f"Saat tıklama hatası: {_te}")
                                        await _sleep(2)

                        # Tarih/Saat seçimi sonrası Continue → Services/Review sayfasına geç
                        for _ba_att in range(10):
                            try:
                                _ba_btn = page.locator('button:has-text("Continue"), button[type="submit"]').first
                                if await _ba_btn.is_visible(timeout=2000):
                                    await _ba_btn.click()
                                    logger.info("Book Appointment Continue tıklandı")
                                    break
                            except Exception:
                                pass
                            await _sleep(2)

                        try:
                            await page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass
                        await _sleep(3)

                        # Services sayfasında Continue tıkla (gerekirse)
                        _srv_body = (await page.locator("body").inner_text()).lower()
                        if "services" in _srv_body or "additional service" in _srv_body:
                            logger.info("Services sayfası tespit edildi — Continue tıklanıyor")
                            for _sva in range(5):
                                try:
                                    _svb = page.locator('button:has-text("Continue"), button[type="submit"]').first
                                    if await _svb.is_visible(timeout=2000):
                                        await _svb.click()
                                        logger.info("Services Continue tıklandı")
                                        break
                                except Exception:
                                    pass
                                await _sleep(2)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=15000)
                            except Exception:
                                pass
                            await _sleep(3)
                else:
                    logger.warning("Your Details Continue butonu tıklanamadı — yine de devam ediliyor")

                # === CAPTCHA (Turnstile) ===
                logger.info("Captcha kontrol ediliyor (form submit öncesi)...")
                for ts_att in range(20):
                    cf_frame = next(
                        (f for f in page.frames if 'challenges.cloudflare.com' in f.url), None
                    )
                    if not cf_frame:
                        logger.info("Captcha yok veya geçildi")
                        break
                    logger.info(f"Captcha Turnstile tıklanıyor (deneme {ts_att+1}/20)...")
                    try:
                        fe = await cf_frame.frame_element()
                        bbox = await fe.bounding_box()
                        if bbox and bbox.get('width', 0) > 0:
                            await page.mouse.click(bbox['x'] + min(22, bbox['width'] / 4), bbox['y'] + bbox['height'] / 2)
                            await _sleep(random.uniform(6.0, 11.0), pct=0.0)
                            continue
                    except Exception as ex:
                        logger.debug(f"Captcha tıklama hatası: {ex}")
                    await _sleep(3)

                # === SUBMIT / CONTINUE ===
                submitted = False
                for sel in ['button:has-text("Continue")', 'button:has-text("Submit")', 'button[type="submit"]']:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3000):
                            await btn.click()
                            logger.info(f"Submit tıklandı: {sel}")
                            submitted = True
                            break
                    except Exception:
                        pass

                if not submitted:
                    await save_error_screenshot("submit_bulunamadi")
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Submit/Continue butonu bulunamadı", "signature": "booking_submit_fail"}

                # Submit sonrası ödeme sayfasını bekle (en fazla 20s)
                for _pay_wait in range(7):
                    await _sleep(3)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    body_after = await page.locator("body").inner_text()
                    cur_url = page.url
                    logger.info(f"Submit sonrası ({_pay_wait+1}/7) URL={cur_url} body={body_after[:120]!r}")

                    # Ödeme sayfasına geldik mi?
                    is_payment_page = (
                        "sanalpos" in cur_url.lower() or
                        "payment" in cur_url.lower() or
                        any(kw in body_after.lower() for kw in ["kart numarası", "card number", "cvv", "kart bilgi", "ödeme bilgi", "pan", "expiry"])
                    )
                    if is_payment_page:
                        logger.info(f"Ödeme sayfası tespit edildi: {cur_url}")
                        await save_error_screenshot("odeme_sayfasi")
                        return await _proceed_payment()  # URL boş → goto yok, zaten buradayız

                    # Hata durumu
                    if any(kw in body_after.lower() for kw in ["error", "hata", "failed", "başarısız"]):
                        break

                body_after = (await page.locator("body").inner_text())[:400]
                logger.info(f"Submit sonrası final: {body_after!r}")
                await save_error_screenshot("booking_submit_sonrasi")

                if notify_func:
                    await notify_func(f"✅ *Başvuru formu gönderildi!*\n\n{body_after[:300]}")

                return {
                    "ok": True,
                    "status_label": "BOOKED",
                    "matched_text": body_after[:200],
                    "signature": hashlib.sha256(f"booked:{body_after[:100]}".encode()).hexdigest()[:16],
                }

            async def _proceed_payment(pay_url: str = "") -> dict:
                """Ödeme sayfasını bot browser'ında işle, kart bilgilerini Telegram'dan al, 3D OTP işle.
                pay_url boşsa zaten ödeme sayfasındayız demektir (goto yapılmaz).
                """
                if not wait_input_func or not notify_func:
                    return {"ok": True, "status_label": "PAYMENT", "matched_text": pay_url or page.url,
                            "signature": hashlib.sha256(f"payment:{pay_url or page.url}".encode()).hexdigest()[:16]}

                # Ödeme sayfasına git (sadece farklı URL ise)
                if pay_url and pay_url != page.url:
                    await notify_func("💳 *Ödeme sayfası açılıyor...*")
                    try:
                        await page.goto(pay_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        logger.warning(f"Ödeme URL navigate hatası: {e}")

                await _sleep(3)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                pay_body = await page.locator("body").inner_text()
                logger.info(f"Ödeme sayfası: {pay_body[:300]!r}")

                # Kart bilgilerini Telegram'dan topla
                await notify_func(
                    "💳 *Ödeme Bilgileri*\n\n"
                    "Lütfen aşağıdaki bilgileri sırasıyla gönderin:\n"
                    "1️⃣ Kart sahibi adı soyadı\n"
                    "2️⃣ Kart numarası (16 hane, boşluksuz)\n"
                    "3️⃣ Son kullanma tarihi (MM/YY formatında)\n"
                    "4️⃣ CVV (kartın arkasındaki 3 hane)\n\n"
                    "⚠️ Bilgiler sadece bu ödeme için kullanılır, kaydedilmez."
                )

                card_name = await wait_input_func("👤 Kart sahibi adı soyadı:", None)
                if not card_name:
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Kart bilgisi girilmedi", "signature": "payment_cancelled"}

                card_number = await wait_input_func("💳 Kart numarası (16 hane):", None)
                if not card_number:
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Kart numarası girilmedi", "signature": "payment_cancelled"}
                card_number = card_number.replace(" ", "").replace("-", "")

                card_expiry = await wait_input_func("📅 Son kullanma tarihi (AA/YY):", None)
                if not card_expiry:
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Son kullanma tarihi girilmedi", "signature": "payment_cancelled"}
                # MM/YY parse
                exp_parts = card_expiry.replace("-", "/").replace(".", "/").split("/")
                exp_month = exp_parts[0].strip().zfill(2) if exp_parts else ""
                exp_year = exp_parts[1].strip() if len(exp_parts) > 1 else ""
                if len(exp_year) == 2:
                    exp_year = "20" + exp_year

                card_cvv = await wait_input_func("🔒 CVV (3 hane):", None)
                if not card_cvv:
                    return {"ok": False, "status_label": "ERROR", "matched_text": "CVV girilmedi", "signature": "payment_cancelled"}
                card_cvv = card_cvv.strip()

                await notify_func("⏳ Kart bilgileri dolduruluyor...")

                # ── iframe tespiti ────────────────────────────────────────────
                # TEB sanalpos ve çoğu 3D ödeme gateway'i formu iframe içinde sunar.
                # Hem doğrudan sayfa hem de bulunan ilk iframe üzerinde doldurma dene.
                async def _get_locator_ctx():
                    """Ödeme formunu barındıran context'i döndürür: iframe varsa frame locator, yoksa page."""
                    await _sleep(2)
                    iframes = page.frames
                    pay_keywords = ["sanalpos", "payment", "pay", "3ds", "secure", "banka", "bank", "teb", "garanti", "isbank", "akbank"]
                    for fr in iframes:
                        fr_url = (fr.url or "").lower()
                        if any(kw in fr_url for kw in pay_keywords) and "challenges.cloudflare" not in fr_url:
                            logger.info(f"Ödeme iframe tespit edildi: {fr.url}")
                            return fr  # frame object — .locator() destekler
                    # Alternatif: src ile iframe elementi bul
                    for kw in pay_keywords:
                        try:
                            fl = page.frame_locator(f'iframe[src*="{kw}"]')
                            # frame_locator geçerli mi kontrol et
                            cnt = await fl.locator("body").count()
                            if cnt > 0:
                                logger.info(f"Ödeme iframe (frame_locator) bulundu: src*={kw}")
                                return fl
                        except Exception:
                            pass
                    logger.info("Ödeme iframe bulunamadı, doğrudan sayfa kullanılacak")
                    return page

                pay_ctx = await _get_locator_ctx()

                async def _fill_pay(sel: str, value: str, label: str, is_select: bool = False) -> bool:
                    """Verilen selector'ı önce iframe context'inde, bulamazsa doğrudan sayfada doldurur."""
                    for ctx in ([pay_ctx, page] if pay_ctx is not page else [page]):
                        try:
                            el = ctx.locator(sel).first
                            if await el.is_visible(timeout=2000):
                                if is_select:
                                    await el.select_option(value=value)
                                else:
                                    await el.fill(value)
                                logger.info(f"{label} dolduruldu [{sel}] (ctx={'iframe' if ctx is not page else 'page'})")
                                return True
                        except Exception:
                            pass
                    return False

                # Kart sahibi adı
                for sel in ['input[name*="cardholder" i]', 'input[name*="pan_name" i]', 'input[id*="cardholder" i]',
                            'input[placeholder*="kart sahibi" i]', 'input[placeholder*="card holder" i]',
                            'input[placeholder*="name on card" i]', '#cardHolderName', '#NameOnCard', 'input[name*="name" i]']:
                    if await _fill_pay(sel, card_name, "Kart sahibi"):
                        break

                # Kart numarası
                for sel in ['input[name*="pan" i]', 'input[name*="cardnumber" i]', 'input[id*="cardnumber" i]',
                            'input[placeholder*="kart numara" i]', 'input[placeholder*="card number" i]',
                            '#cardNumber', '#pan', 'input[maxlength="16"]', 'input[maxlength="19"]']:
                    if await _fill_pay(sel, card_number, "Kart numarası"):
                        break

                # Son kullanma ay
                for sel in ['select[name*="month" i]', 'select[id*="month" i]', '#expMonth', '#Ay']:
                    if await _fill_pay(sel, exp_month, "Ay (select)", is_select=True):
                        break
                else:
                    for sel in ['input[name*="exp_month" i]', 'input[name*="expmonth" i]',
                                'input[placeholder*="ay" i]', 'input[placeholder*="mm" i]', 'input[id*="month" i]']:
                        if await _fill_pay(sel, exp_month, "Ay"):
                            break

                # Son kullanma yıl
                _exp_year_short = exp_year[-2:] if len(exp_year) == 4 else exp_year
                for sel in ['select[name*="year" i]', 'select[id*="year" i]', '#expYear', '#Yil']:
                    if await _fill_pay(sel, exp_year, "Yıl (select)", is_select=True):
                        break
                    if await _fill_pay(sel, _exp_year_short, "Yıl (select kısa)", is_select=True):
                        break
                else:
                    for sel in ['input[name*="exp_year" i]', 'input[name*="expyear" i]',
                                'input[placeholder*="yil" i]', 'input[placeholder*="yy" i]', 'input[id*="year" i]']:
                        if await _fill_pay(sel, _exp_year_short, "Yıl"):
                            break

                # CVV
                for sel in ['input[name*="cvv" i]', 'input[name*="cvc" i]', 'input[name*="cv2" i]',
                            'input[placeholder*="cvv" i]', 'input[placeholder*="güvenlik" i]',
                            '#cvv', '#cvv2', '#cvc', 'input[maxlength="3"]', 'input[maxlength="4"]']:
                    if await _fill_pay(sel, card_cvv, "CVV"):
                        break

                # Tutar varsa bildir
                try:
                    pay_body2 = await page.locator("body").inner_text()
                    amount_m = re.search(r'[\d.,]+\s*(TL|TRY|EUR|USD|₺)', pay_body2)
                    if amount_m:
                        await notify_func(f"💰 Ödeme tutarı: *{amount_m.group(0)}*\n\nÖdeme gönderiliyor...")
                except Exception:
                    pass

                # Submit — önce iframe context'inde, sonra doğrudan sayfada dene
                await _sleep(1)
                submit_clicked = False
                sub_sels = ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Ödeme Yap")',
                            'button:has-text("Pay")', 'button:has-text("Onayla")', 'button:has-text("Confirm")',
                            'button:has-text("Tamam")', 'a:has-text("Ödeme Yap")', '.payment-button', '#payButton']
                for ctx in ([pay_ctx, page] if pay_ctx is not page else [page]):
                    for sub_sel in sub_sels:
                        try:
                            btn = ctx.locator(sub_sel).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                logger.info(f"Ödeme submit tıklandı: {sub_sel}")
                                submit_clicked = True
                                break
                        except Exception:
                            pass
                    if submit_clicked:
                        break

                if not submit_clicked:
                    await notify_func("❌ Ödeme butonu bulunamadı.")
                    return {"ok": False, "status_label": "ERROR", "matched_text": "Submit butonu yok", "signature": "payment_submit_error"}

                await notify_func("⏳ 3D Secure bekleniyor...")

                # 3D Secure sayfasını bekle (en fazla 30s)
                # 3D sayfası: bazen yeni iframe, bazen tam sayfa yönlendirme olabilir.
                # Her turda iframe context'ini yeniden tespit et.
                _3d_ctx = page  # başlangıç: doğrudan sayfa
                for _3d_wait in range(10):
                    await _sleep(3)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    # 3D iframe yeniden tespiti (kart submit sonrası yeni frame çıkabilir)
                    _3d_ctx = await _get_locator_ctx()

                    # Body metnini hem ana sayfadan hem iframe'den almayı dene
                    body_3d = ""
                    try:
                        body_3d = await page.locator("body").inner_text()
                    except Exception:
                        pass
                    if _3d_ctx is not page:
                        try:
                            iframe_body = await _3d_ctx.locator("body").inner_text()
                            body_3d = body_3d + " " + iframe_body
                        except Exception:
                            pass
                    body_3d_low = body_3d.lower()
                    logger.info(f"3D bekleniyor ({_3d_wait+1}/10): {body_3d[:120]!r}")

                    # 3D OTP input var mı? — önce iframe context'inde, sonra sayfada ara
                    has_otp = any(kw in body_3d_low for kw in ["otp", "şifre", "parola", "doğrulama kodu", "sms", "one time"])
                    otp_inp = None
                    otp_ctx = page  # OTP input hangi context'te bulundu
                    _otp_sels = ['input[name*="otp" i]', 'input[name*="password" i]', 'input[type="password"]',
                                 'input[placeholder*="otp" i]', 'input[placeholder*="şifre" i]',
                                 'input[placeholder*="kod" i]', 'input[name*="verificationCode" i]']
                    for _ctx_candidate in (([_3d_ctx, page] if _3d_ctx is not page else [page])):
                        for otp_sel in _otp_sels:
                            try:
                                el = _ctx_candidate.locator(otp_sel).first
                                if await el.is_visible(timeout=1000):
                                    otp_inp = otp_sel
                                    otp_ctx = _ctx_candidate
                                    has_otp = True
                                    break
                            except Exception:
                                pass
                        if otp_inp:
                            break

                    if has_otp and otp_inp:
                        # Kullanıcıdan OTP al
                        otp_code = await wait_input_func(
                            "📱 *3D Secure Doğrulama*\n\nTelefonunuza SMS ile gelen kodu girin:", None
                        )
                        if not otp_code:
                            return {"ok": False, "status_label": "ERROR", "matched_text": "3D OTP girilmedi", "signature": "3d_otp_cancelled"}

                        try:
                            await otp_ctx.locator(otp_inp).first.fill(otp_code.strip())
                            logger.info(f"3D OTP girildi: {otp_inp} (ctx={'iframe' if otp_ctx is not page else 'page'})")
                        except Exception as e:
                            logger.warning(f"OTP girme hatası: {e}")

                        # 3D submit — OTP bulduğumuz context'te, yoksa sayfada dene
                        _sub3d_sels = ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Onayla")',
                                       'button:has-text("Doğrula")', 'button:has-text("Confirm")', 'button:has-text("Verify")']
                        for _sub3d_ctx in ([otp_ctx, page] if otp_ctx is not page else [page]):
                            for sub3d_sel in _sub3d_sels:
                                try:
                                    btn3d = _sub3d_ctx.locator(sub3d_sel).first
                                    if await btn3d.is_visible(timeout=2000):
                                        await btn3d.click()
                                        logger.info(f"3D submit tıklandı: {sub3d_sel}")
                                        break
                                except Exception:
                                    pass

                        await _sleep(5)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass

                        final_pay_body = await page.locator("body").inner_text()
                        final_low = final_pay_body.lower()
                        logger.info(f"3D sonrası sayfa: {final_pay_body[:300]!r}")

                        # Başarı/hata tespiti
                        if any(kw in final_low for kw in ["başarılı", "onaylandı", "approved", "success", "tamamlandı", "confirmed", "booking confirmed"]):
                            await notify_func(f"✅ *Ödeme Başarılı!*\n\nRandevunuz onaylandı.")
                            return {"ok": True, "status_label": "BOOKED", "matched_text": "Ödeme tamamlandı",
                                    "signature": hashlib.sha256(f"paid:{page.url}".encode()).hexdigest()[:16]}
                        elif any(kw in final_low for kw in ["başarısız", "hata", "red", "declined", "failed", "error", "invalid"]):
                            await notify_func(f"❌ *Ödeme Başarısız!*\n\n{final_pay_body[:200]}")
                            return {"ok": False, "status_label": "ERROR", "matched_text": final_pay_body[:100], "signature": "payment_failed"}
                        else:
                            await notify_func(f"⚠️ *Ödeme Sonucu Belirsiz*\n\n{final_pay_body[:300]}")
                            return {"ok": True, "status_label": "PENDING", "matched_text": final_pay_body[:100],
                                    "signature": hashlib.sha256(f"pay_unknown:{page.url}".encode()).hexdigest()[:16]}

                    # Zaten tamamlandıysa
                    if any(kw in body_3d_low for kw in ["başarılı", "onaylandı", "approved", "success", "booking confirmed"]):
                        await notify_func("✅ *Ödeme Başarılı!*\n\nRandevunuz onaylandı.")
                        return {"ok": True, "status_label": "BOOKED", "matched_text": "Ödeme tamamlandı",
                                "signature": hashlib.sha256(f"paid:{page.url}".encode()).hexdigest()[:16]}

                await notify_func(f"⚠️ 3D Secure ekranı tespit edilemedi.\nSayfa: {(await page.locator('body').inner_text())[:200]}")
                return {"ok": True, "status_label": "PAYMENT", "matched_text": pay_url,
                        "signature": hashlib.sha256(f"payment:{pay_url}".encode()).hexdigest()[:16]}

            async def select_mat_option(dropdown_sel: str, option_text: str, label: str) -> bool:
                """Mat-select dropdown'ı aç ve seçenek tıkla.
                Returns True if option found and clicked, False if dropdown has no options (= no slots).
                Raises only on unexpected errors (e.g. dropdown not clickable).
                """
                logger.info(f"{label} seçiliyor: {option_text!r}")
                # Dropdown'ın enabled olmasını bekle (Angular disabled kaldırana kadar)
                for _ in range(30):
                    el = page.locator(dropdown_sel)
                    try:
                        disabled = await el.get_attribute("aria-disabled", timeout=3000)
                    except Exception:
                        disabled = None
                    if disabled != "true":
                        break
                    await _sleep(1)

                # DOM diagnostic: mat-select içindeki ve global mat-option sayısını logla
                _diag = await page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return {found: false};
                    const inEl = el.querySelectorAll('mat-option').length;
                    const global = document.querySelectorAll('mat-option').length;
                    const allSels = document.querySelectorAll('mat-select');
                    const disabled = el.getAttribute('aria-disabled');
                    const ctxType = typeof el.__ngContext__;
                    // Try window.ng API
                    let ngComp = null;
                    try { ngComp = window.ng && window.ng.getComponent ? typeof window.ng.getComponent(el) : 'no-ng'; } catch(e) {}
                    // innerHTML snippet
                    const inner = el.innerHTML.substring(0, 200);
                    return {found: true, inEl, global, disabled, ctxType, ngComp,
                            selCount: allSels.length, inner};
                }""", dropdown_sel)
                logger.info(f"{label} DOM diag: {_diag}")

                # Angular'ın mat-option child'larını render etmesini bekle
                # mat-select.open() içinde this.options.length===0 ise panel açılmıyor!
                for _ow in range(5):
                    _child_count = await page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return -1;
                        // Direct children inside mat-select
                        const inEl = el.querySelectorAll('mat-option').length;
                        // Global mat-option elements (may be outside mat-select)
                        const global = document.querySelectorAll('mat-option').length;
                        // __ngContext__ options QueryList
                        const ctx = el.__ngContext__;
                        if (ctx) {
                            const items = Array.isArray(ctx) ? ctx : Object.values(ctx);
                            for (const c of items) {
                                if (c && c.options && c.options._results && c.options._results.length > 0)
                                    return c.options._results.length;
                            }
                        }
                        // window.ng component
                        try {
                            if (window.ng && window.ng.getComponent) {
                                const comp = window.ng.getComponent(el);
                                if (comp && comp.options && comp.options._results)
                                    return comp.options._results.length;
                            }
                        } catch(e) {}
                        return Math.max(inEl, global);
                    }""", dropdown_sel)
                    if _child_count > 0:
                        logger.info(f"{label} Angular mat-option'lar hazir ({_child_count} adet, {_ow}s bekledik)")
                        break
                    if _ow == 0:
                        logger.info(f"{label} Angular mat-option'lar bekleniyor...")
                    await _sleep(1)
                else:
                    logger.warning(f"{label} 5s icinde Angular mat-option'lar gelmedi — yine de denenecek")

                # Önce JS ile MatOption.select() doğrudan dene (DOM açmadan)
                _js_direct_select = await page.evaluate("""([sel, target]) => {
                    const el = document.querySelector(sel);
                    if (!el) return {ok: false, reason: 'no element'};
                    const ctx = el.__ngContext__;
                    if (!ctx) return {ok: false, reason: 'no ctx'};
                    const items = Array.isArray(ctx) ? ctx : Object.values(ctx);
                    for (const c of items) {
                        if (!c || typeof c !== 'object') continue;
                        const ql = c.options;
                        if (!ql) continue;
                        const results = ql._results || (Array.isArray(ql) ? ql : []);
                        if (results.length === 0) continue;
                        const tLow = target.toLowerCase();
                        const match = results.find(o =>
                            (o.viewValue || '').toLowerCase().includes(tLow) ||
                            tLow.includes((o.viewValue || '').toLowerCase())
                        );
                        if (match && typeof match.select === 'function') {
                            match.select();
                            try {
                                const injector = el.__ngContext__?.find?.(x => x?.get);
                                if (injector) {
                                    const ngZone = injector.get?.(window.ng?.core?.NgZone);
                                    if (ngZone?.run) ngZone.run(() => {});
                                }
                            } catch(e) {}
                            return {ok: true, selected: match.viewValue};
                        }
                        return {ok: false, reason: 'no match', opts: results.map(o => o.viewValue || '')};
                    }
                    return {ok: false, reason: 'no options in ctx'};
                }""", [dropdown_sel, option_text])
                if isinstance(_js_direct_select, dict) and _js_direct_select.get("ok"):
                    logger.info(f"{label} JS MatOption.select() başarılı: {_js_direct_select.get('selected')!r}")
                    await _sleep(2)
                    return True
                else:
                    logger.info(f"{label} JS MatOption.select() denendi: {_js_direct_select}")

                # Dropdown'ı aç ve mat-option'ların gelmesini bekle — en fazla 5 deneme
                for attempt in range(5):
                    # Önce JS ile MatSelect.open() dene: __ngContext__ veya window.ng
                    _opened_via_js = await page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return 'no-el';
                        // Try window.ng.getComponent first (works even in prod builds)
                        try {
                            if (window.ng && window.ng.getComponent) {
                                const comp = window.ng.getComponent(el);
                                if (comp && typeof comp.open === 'function') {
                                    comp.open();
                                    return 'ng-api';
                                }
                            }
                        } catch(e) {}
                        // Try __ngContext__
                        const ctx = el.__ngContext__;
                        if (!ctx) return 'no-ctx';
                        const items = Array.isArray(ctx) ? ctx : Object.values(ctx);
                        for (const c of items) {
                            if (c && typeof c.open === 'function' && c.options !== undefined) {
                                c.open();
                                return 'ctx-open';
                            }
                        }
                        return 'ctx-no-match';
                    }""", dropdown_sel)
                    if _opened_via_js in ('ng-api', 'ctx-open'):
                        logger.info(f"{label} MatSelect.open() JS ile acildi: {_opened_via_js} (deneme {attempt+1})")
                    if _opened_via_js not in ('ng-api', 'ctx-open'):
                        # Scroll + focus + click kombinasyonu
                        try:
                            await page.locator(dropdown_sel).scroll_into_view_if_needed(timeout=5000)
                        except Exception:
                            pass
                        # Önce force=True ile dene (başka element kapıyor olabilir)
                        # Sonra dispatchEvent ile Angular'a direkt click gönder
                        try:
                            await page.locator(dropdown_sel).click(timeout=10000, force=True)
                        except Exception:
                            pass
                        # Angular zone içinde dispatchEvent
                        await page.evaluate("""(sel) => {
                            const el = document.querySelector(sel);
                            if (!el) return;
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles: true, cancelable: true}));
                            el.dispatchEvent(new MouseEvent('click',     {bubbles: true, cancelable: true}));
                        }""", dropdown_sel)

                    # mat-option'ların DOM'a gelmesini bekle (en fazla 8sn)
                    try:
                        await page.wait_for_selector(
                            "mat-option, .mat-mdc-option, .cdk-overlay-container [role='option']",
                            state="attached", timeout=8000
                        )
                    except Exception:
                        pass

                    # Tüm mevcut seçenekleri kontrol et (visibility'den bağımsız, MDC ve eski stil)
                    all_opts = await page.evaluate("""() => {
                        const sels = [
                            'mat-option',
                            '.mat-mdc-option',
                            '.cdk-overlay-container [role="option"]',
                            '.cdk-overlay-pane [role="option"]'
                        ];
                        const seen = new Set();
                        const result = [];
                        for (const sel of sels) {
                            document.querySelectorAll(sel).forEach(o => {
                                const txt = (o.textContent || '').trim();
                                if (txt && !seen.has(txt)) { seen.add(txt); result.push(txt); }
                            });
                        }
                        return result;
                    }""")
                    try:
                        _expanded = await page.locator(dropdown_sel).get_attribute("aria-expanded", timeout=3000)
                    except Exception:
                        _expanded = None
                    _overlay_html = await page.evaluate("""() => {
                        const c = document.querySelector('.cdk-overlay-container');
                        return c ? c.innerHTML.substring(0, 300) : 'no overlay';
                    }""")
                    logger.info(f"{label} için {len(all_opts)} seçenek (deneme {attempt+1}), expanded={_expanded}, overlay={_overlay_html[:120]!r}")

                    if not all_opts:
                        # Keyboard fallback: focus + space to open, then arrow down, then enter
                        try:
                            await page.locator(dropdown_sel).focus()
                            await _sleep(0.3)
                            await page.keyboard.press("Space")
                            await _sleep(1)
                            _kbd_opts = await page.evaluate("""() => {
                                const sels = ['mat-option', '.mat-mdc-option', '.cdk-overlay-container [role="option"]'];
                                const seen = new Set(), result = [];
                                for (const sel of sels) {
                                    document.querySelectorAll(sel).forEach(o => {
                                        const t = (o.textContent||'').trim();
                                        if (t && !seen.has(t)) { seen.add(t); result.push(t); }
                                    });
                                }
                                return result;
                            }""")
                            if _kbd_opts:
                                logger.info(f"{label} keyboard+Space ile {len(_kbd_opts)} seçenek açıldı: {_kbd_opts[:5]}")
                                all_opts = _kbd_opts
                        except Exception as _ke:
                            logger.debug(f"{label} keyboard fallback hata: {_ke}")

                    if not all_opts:
                        await page.keyboard.press("Escape")
                        await _sleep(3)
                        continue

                    # İstenen seçeneği JS ile tıkla (MDC + eski stil, Playwright locator'a güvenmeden)
                    _js_clicked = await page.evaluate("""([target]) => {
                        const tLow = target.toLowerCase();
                        // ilk "/" öncesi kısım (Türkçe bölümün ilk 3 kelimesi)
                        const tPrefix = tLow.split('/')[0].trim().split(/ +/).slice(0, 3).join(' ');
                        const sels = ['mat-option', '.mat-mdc-option', '.cdk-overlay-container [role="option"]', '.cdk-overlay-pane [role="option"]'];
                        // Geçiş 1: exact/contains
                        for (const sel of sels) {
                            for (const o of document.querySelectorAll(sel)) {
                                const txt = (o.textContent || '').trim().toLowerCase();
                                if (txt && (txt.includes(tLow) || tLow.includes(txt))) {
                                    o.click();
                                    return {ok: true, clicked: o.textContent?.trim(), sel};
                                }
                            }
                        }
                        // Geçiş 2: ilk 3 kelime prefix eşleşmesi
                        if (tPrefix.length > 4) {
                            for (const sel of sels) {
                                for (const o of document.querySelectorAll(sel)) {
                                    const txt = (o.textContent || '').trim().toLowerCase();
                                    if (txt && txt.startsWith(tPrefix)) {
                                        o.click();
                                        return {ok: true, clicked: o.textContent?.trim(), sel, method: 'prefix'};
                                    }
                                }
                            }
                        }
                        const allTxts = Array.from(document.querySelectorAll('mat-option, .mat-mdc-option')).map(o => o.textContent?.trim());
                        return {ok: false, opts: allTxts};
                    }""", [option_text])
                    if isinstance(_js_clicked, dict) and _js_clicked.get("ok"):
                        logger.info(f"{label} JS click başarılı: {_js_clicked.get('clicked')!r}")
                        # JS click sonrası Playwright ile de tıkla — Angular zone.js'in form modelini güncellemesi için
                        _opt_search = option_text[:60] if len(option_text) > 60 else option_text
                        try:
                            _pw_loc = page.locator(f'mat-option:has-text("{_opt_search}")')
                            _pw_cnt = await _pw_loc.count()
                            if _pw_cnt > 0:
                                await _pw_loc.first.click(timeout=3000)
                                logger.info(f"{label} Playwright click de yapıldı — Angular form güncellenmiş olmalı")
                        except Exception as _pw_e:
                            logger.debug(f"{label} Playwright click hatası (sorun değil): {_pw_e}")
                        await _sleep(3)
                        return True

                    # Playwright locator fallback
                    opt_loc = page.locator(f'mat-option:has-text("{option_text}")')
                    cnt = await opt_loc.count()
                    if cnt > 0:
                        await opt_loc.first.click(timeout=15000)
                        await _sleep(3)
                        return True

                    # Seçenekler var ama istenen yok — partial match dene
                    opt_partial = page.locator("mat-option").filter(has_text=option_text)
                    cnt_p = await opt_partial.count()
                    if cnt_p > 0:
                        logger.info(f"{label} partial match ile bulundu")
                        await opt_partial.first.click(timeout=15000)
                        await _sleep(3)
                        return True

                    logger.warning(f"{label}: seçenek listede yok. JS sonuç: {_js_clicked}")
                    await page.keyboard.press("Escape")
                    await _sleep(3)

                # 5 denemede de seçenek yok (API boş döndü) → CLOSED
                logger.warning(f"{label}: {option_text!r} 5 denemede bulunamadı — CLOSED olarak değerlendiriliyor")
                await save_error_screenshot(f"mat_select_bulunamadi_{label[:20].replace(' ','_')}")
                return False

            async def _js_read_angular_options(dropdown_sel: str) -> list[dict]:
                """Angular mat-select bileşeninin options listesini DOM açmadan JS ile oku."""
                try:
                    result = await page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return [];
                        const ctx = el.__ngContext__;
                        if (!ctx || !Array.isArray(ctx)) return [];
                        for (let i = 0; i < ctx.length; i++) {
                            const c = ctx[i];
                            if (!c || typeof c !== 'object') continue;
                            const optList = (c.options && (c.options._results || (typeof c.options.forEach === 'function' ? c.options : null)));
                            if (optList) {
                                const items = Array.isArray(optList) ? optList : (c.options._results || []);
                                if (items.length > 0) {
                                    return items.map(o => ({
                                        value: String(o.value ?? ''),
                                        text: String(o.viewValue || o.value || '')
                                    }));
                                }
                            }
                        }
                        return [];
                    }""", dropdown_sel)
                    return result if isinstance(result, list) else []
                except Exception as e:
                    logger.debug(f"JS Angular options okuma hatası: {e}")
                    return []

            async def _js_set_formcontrol(form_control_name: str, value: str) -> bool:
                """Angular ReactiveForm FormControl değerini JS ile doğrudan ayarla."""
                try:
                    result = await page.evaluate("""([name, val]) => {
                        const el = document.querySelector(`[formcontrolname="${name}"]`);
                        if (!el) return {error: 'no element: ' + name};

                        // --- Helper: ctx'ten ngCtrl ve matSelect çıkar ---
                        function extractFromCtx(ctx) {
                            let ngCtrl = null, matSelect = null;
                            if (!ctx) return {ngCtrl, matSelect};
                            const items = Array.isArray(ctx) ? ctx : Object.values(ctx);
                            for (const c of items) {
                                if (!c || typeof c !== 'object') continue;
                                if (!ngCtrl && c.control && typeof c.control.setValue === 'function') ngCtrl = c;
                                if (!matSelect && typeof c.writeValue === 'function' && c.options !== undefined) matSelect = c;
                            }
                            return {ngCtrl, matSelect};
                        }

                        // 1) Element üzerindeki __ngContext__ (Ivy LView)
                        let {ngCtrl, matSelect} = extractFromCtx(el.__ngContext__);

                        // 2) window.ng debug API (Angular DevTools)
                        if (!ngCtrl && !matSelect && window.ng) {
                            try {
                                const comp = window.ng.getComponent?.(el) || window.ng.getContext?.(el);
                                if (comp) ({ ngCtrl, matSelect } = extractFromCtx(Object.values(comp)));
                            } catch(e) {}
                        }

                        // 3) Parent elementlerde FormGroup ara (form genellikle parent'ta)
                        if (!ngCtrl) {
                            let cur = el.parentElement;
                            for (let p = 0; p < 20 && cur; p++, cur = cur.parentElement) {
                                const pCtx = cur.__ngContext__;
                                if (!pCtx) continue;
                                const items = Array.isArray(pCtx) ? pCtx : Object.values(pCtx);
                                for (const c of items) {
                                    if (!c || typeof c !== 'object') continue;
                                    // FormGroup
                                    if (c.controls && c.controls[name]) {
                                        ngCtrl = {control: c.controls[name]};
                                        break;
                                    }
                                    // NgForm
                                    if (c.form && c.form.controls && c.form.controls[name]) {
                                        ngCtrl = {control: c.form.controls[name]};
                                        break;
                                    }
                                    // FormGroupDirective
                                    if (c._form && c._form.controls && c._form.controls[name]) {
                                        ngCtrl = {control: c._form.controls[name]};
                                        break;
                                    }
                                }
                                if (ngCtrl) break;
                            }
                        }

                        // 4) matSelect: element üzerinde doğrudan
                        if (!matSelect) {
                            const ctx = el.__ngContext__;
                            if (ctx) {
                                const items = Array.isArray(ctx) ? ctx : Object.values(ctx);
                                for (const c of items) {
                                    if (c && typeof c.open === 'function' && typeof c.close === 'function') {
                                        matSelect = c;
                                        break;
                                    }
                                }
                            }
                        }

                        let method = 'none';
                        const ctxDebug = `el.ctx=${typeof el.__ngContext__}(isArr=${Array.isArray(el.__ngContext__)})`;

                        // Apply: FormControl.setValue
                        if (ngCtrl) {
                            try {
                                ngCtrl.control.setValue(val, {emitEvent: true, emitModelToViewChange: true, emitViewToModelChange: true});
                                ngCtrl.control.markAsDirty();
                                ngCtrl.control.updateValueAndValidity({emitEvent: true});
                                method = 'formControl.setValue';
                            } catch(e) { method = 'formControl.err:' + e.message; }
                        }

                        // Apply: MatSelect.writeValue + selectionChange
                        if (matSelect) {
                            try { matSelect.writeValue(val); } catch(e) {}
                            try { if (typeof matSelect._onChange === 'function') matSelect._onChange(val); } catch(e) {}
                            try { if (matSelect.stateChanges?.next) matSelect.stateChanges.next(); } catch(e) {}
                            try {
                                if (matSelect.selectionChange?.emit)
                                    matSelect.selectionChange.emit({source: matSelect, value: val});
                            } catch(e) {}
                            method = method === 'none' ? 'matSelect.writeValue' : method + '+matSelect';
                        }

                        // Dispatch native input/change events
                        try { el.dispatchEvent(new Event('change', {bubbles: true})); } catch(e) {}
                        try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch(e) {}

                        // Trigger Angular change detection
                        try {
                            const appEl = document.querySelector('[ng-version]') || document.body;
                            const injector = window.ng?.getInjector?.(appEl);
                            if (injector) {
                                const appRef = injector.get?.(window.ng?.core?.ApplicationRef);
                                if (appRef?.tick) appRef.tick();
                            }
                        } catch(e) {}

                        if (method === 'none') return {error: 'no control found', debug: ctxDebug};
                        return {ok: true, method, debug: ctxDebug};
                    }""", [form_control_name, value])
                    ok = isinstance(result, dict) and result.get("ok") is True
                    logger.info(f"JS FormControl '{form_control_name}'={value!r}: {result}")
                    return ok
                except Exception as e:
                    logger.error(f"JS FormControl set hatası '{form_control_name}': {e}")
                    return False

            async def _select_with_js_fallback(dropdown_sel: str, form_ctrl_name: str, option_text: str, label: str, api_data: list[dict]) -> bool:
                """
                Önce Angular JS yaklaşımıyla seçer (API'den alınan data veya bileşen options listesi).
                Başarısız olursa DOM click ile dener.
                """
                # 1) API verisi varsa önce kodu bul
                _code = None
                for item in api_data:
                    # Tüm olası code/name key kombinasyonlarını dene
                    code_val = (item.get("visaCategoryCode") or item.get("categoryCode") or
                                item.get("subCategoryCode") or item.get("code") or
                                item.get("visaTypeCode") or item.get("typeCode") or "")
                    name_val = (item.get("visaCategoryDesc") or item.get("categoryDesc") or
                                item.get("subCategoryDesc") or item.get("name") or
                                item.get("description") or item.get("visaCategoryName") or
                                item.get("categoryName") or "")
                    if not name_val or not code_val:
                        continue
                    if option_text.lower() in name_val.lower() or name_val.lower() in option_text.lower():
                        _code = str(code_val)
                        logger.info(f"{label} API kodu bulundu: {name_val!r} → {_code!r}")
                        break

                # 2) Angular bileşen options listesinden veya parent component data'sından bul
                if not _code:
                    js_opts = await _js_read_angular_options(dropdown_sel)
                    # mat-option QueryList boşsa parent component'in veri listesini dene
                    if not js_opts:
                        try:
                            js_opts = await page.evaluate("""(sel) => {
                                // Parent component'teki kategori listelerini bul
                                const el = document.querySelector(sel);
                                if (!el) return [];
                                // Mat-select'in formcontrolname'ini al
                                const fcName = el.getAttribute('formcontrolname') || '';
                                // Tüm Angular context'leri tara (mat-select ve parent'ları)
                                let searchEl = el;
                                for (let depth = 0; depth < 8; depth++) {
                                    const ctx = searchEl.__ngContext__;
                                    if (ctx && Array.isArray(ctx)) {
                                        for (let i = 0; i < ctx.length; i++) {
                                            const c = ctx[i];
                                            if (!c || typeof c !== 'object') continue;
                                            // Tüm array property'leri tara — kategori listesi olabilecekleri bul
                                            for (const key of Object.keys(c)) {
                                                const val = c[key];
                                                if (!Array.isArray(val) || val.length === 0) continue;
                                                const f = val[0];
                                                if (!f || typeof f !== 'object') continue;
                                                // Code + description pattern'i ara
                                                const keys = Object.keys(f).join(' ').toLowerCase();
                                                if ((keys.includes('code') || keys.includes('Code')) &&
                                                    (keys.includes('desc') || keys.includes('name'))) {
                                                    const codeKey = Object.keys(f).find(k => k.toLowerCase().includes('code'));
                                                    const nameKey = Object.keys(f).find(k =>
                                                        k.toLowerCase().includes('desc') || k.toLowerCase().includes('name'));
                                                    if (codeKey && nameKey) {
                                                        return val.map(o => ({
                                                            value: String(o[codeKey] || ''),
                                                            text: String(o[nameKey] || '')
                                                        })).filter(o => o.value && o.text);
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    searchEl = searchEl.parentElement;
                                    if (!searchEl) break;
                                }
                                return [];
                            }""", dropdown_sel)
                        except Exception as _je:
                            logger.debug(f"{label} JS parent data okuma hatası: {_je}")
                            js_opts = []
                    if js_opts:
                        logger.info(f"{label} JS Angular seçenekleri ({len(js_opts)} adet): {js_opts[:3]}")
                        for opt in js_opts:
                            if option_text.lower() in opt["text"].lower() or opt["text"].lower() in option_text.lower():
                                _code = opt["value"]
                                logger.info(f"{label} JS Angular kodu bulundu: {opt['text']!r} → {_code!r}")
                                break
                        if not _code and js_opts:
                            # Tam eşleşme yoksa ilk seçeneği almayı dene (display text = value ise)
                            _code = js_opts[0]["value"]
                            logger.warning(f"{label} Tam eşleşme yok, ilk JS seçenek kullanılıyor: {js_opts[0]}")

                # 3) Kodu JS ile FormControl'e yaz
                if _code:
                    ok = await _js_set_formcontrol(form_ctrl_name, _code)
                    if not ok:
                        # Kod ile başarısız, display text ile dene
                        ok = await _js_set_formcontrol(form_ctrl_name, option_text)
                    if ok:
                        await _sleep(1)
                        # JS FormControl değeri ayarladı. Aynı zamanda DOM click ile mat-select
                        # görsel durumunu güncelle. FormControl değeri ayarlansa bile mat-select
                        # seçimi görsel olarak güncellemez ve Continue butonu disabled kalır.
                        # DOM click ile overlay açılır, seçenek tıklanır, görsel güncellenir.
                        logger.info(f"{label} JS set OK; mat-select DOM görsel güncellemesi yapılıyor...")
                        _dom_ok = await select_mat_option(dropdown_sel, option_text, label)
                        if _dom_ok:
                            logger.info(f"{label} JS+DOM seçimi başarılı — Continue aktif olmalı")
                        else:
                            logger.warning(f"{label} JS set OK ama DOM görsel güncelleme başarısız — Continue disabled kalabilir")
                        return True

                # 4) DOM click fallback (kod bulunamadı veya her iki JS denemesi başarısız)
                logger.info(f"{label} JS yaklaşımı başarısız/kod yok, DOM click deneniyor...")
                return await select_mat_option(dropdown_sel, option_text, label)

            async def _fetch_subcats_direct(center_code: str, cat_code: str) -> list[dict]:
                """VFS API'sinden subvisacategory verilerini doğrudan çek (Angular event'i tetiklenmese bile)."""
                if not center_code or not cat_code:
                    return []
                try:
                    # URL'den missionCode/languageCode/countryCode çıkar
                    # Örnek: https://visa.vfsglobal.com/tur/en/fra/login
                    import re as _re
                    _url = site.get("url", "")
                    _m = _re.search(r'vfsglobal\.com/([a-z]+)/([a-z-]+)/([a-z]+)/', _url)
                    if _m:
                        mission_code, _lang, country_code = _m.group(1), _m.group(2), _m.group(3)
                    else:
                        mission_code, country_code = "tur", "fra"
                    sub_url = (f"https://lift-api.vfsglobal.com/master/subvisacategory/"
                               f"{country_code}/{mission_code}/{center_code}/{cat_code}/en-US")
                    extra_headers = {}
                    if _vfs_api_headers.get("clientsource"):
                        extra_headers["clientsource"] = _vfs_api_headers["clientsource"]
                    if _vfs_api_headers.get("authorize"):
                        extra_headers["authorize"] = _vfs_api_headers["authorize"]
                    if _vfs_api_headers.get("route"):
                        extra_headers["route"] = _vfs_api_headers["route"]
                    extra_headers["accept"] = "application/json, text/plain, */*"
                    extra_headers["referer"] = f"https://visa.vfsglobal.com/{mission_code}/en/{country_code}/application-detail"
                    extra_headers["origin"] = "https://visa.vfsglobal.com"
                    logger.info(f"Subcategory API doğrudan çağrılıyor: {sub_url}")
                    resp = await page.request.get(sub_url, headers=extra_headers)
                    if resp.status == 200:
                        data = await resp.json()
                        items = data if isinstance(data, list) else (data.get("data") or [])
                        if items and isinstance(items, list) and isinstance(items[0], dict):
                            logger.info(f"✅ Subcategory API direkt ({len(items)} adet): {[i.get('name') or i.get('code') for i in items]}")
                            _vfs_api_subcats.clear()
                            _vfs_api_subcats.extend(items)
                            return items
                    logger.warning(f"Subcategory API direkt HTTP {resp.status}: {sub_url}")
                except Exception as _fe:
                    logger.warning(f"Subcategory API direkt hata: {_fe}")
                return []

            # === Merkez seçim döngüsü: slot yoksa başka merkez seçilebilir ===
            _appt_detail_url = (
                (site.get("url", "").split("/book-appointment")[0] + "/book-appointment/application-detail")
                if "/book-appointment" in site.get("url", "") else page.url
            )
            _sel_center = center_text
            _sel_category = category_text
            _sel_subcategory = subcategory_text

            async def _ask_retry_center(msg: str) -> bool:
                """Slot yok: kullanıcıya sor. True=başka merkez dene, False=iptal."""
                if not (booking_mode and wait_input_func):
                    return False
                _r = await wait_input_func(
                    msg,
                    [
                        [InlineKeyboardButton("🔄 Başka Merkez Kontrol Et", callback_data="vfs_pick:__retry_center__")],
                        [InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")],
                    ],
                )
                return _r == "__retry_center__"

            while True:
                center_text = _sel_center
                category_text = _sel_category
                subcategory_text = _sel_subcategory
                _vfs_api_cats.clear()
                _vfs_api_subcats.clear()

                # Merkez retry sonrası navigate edilmişse Angular formun hazır olmasını bekle.
                # _sel_center None ise ya ilk iterasyon ya da retry — her iki durumda da
                # center dropdown'ının visible olmasını beklemek zararsız (ilk iterasyonda zaten hazır).
                if not center_text:
                    _cw_ready = await wait_first_visible(page, [center_sel], timeout_ms=30000)
                    if not _cw_ready:
                        logger.warning("Merkez dropdown 30s içinde yüklenemedi (retry sonrası navigate)")
                    else:
                        # networkidle bekle — Angular API çağrılarının tamamlanması için
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass

                # === BOOKING MODUNDA: merkez daha önce seçilmediyse VFS'den çekip sor ===
                if booking_mode and wait_input_func and not center_text:
                    center_options = []
                    for _cattempt in range(4):
                        try:
                            await page.locator(center_sel).click(timeout=15000)
                            try:
                                await page.wait_for_selector("mat-option", state="visible", timeout=15000)
                            except Exception:
                                pass
                            center_options = [t.strip() for t in await page.locator("mat-option").all_inner_texts() if t.strip()]
                            await page.keyboard.press("Escape")
                            await _sleep(1)
                            if center_options:
                                break
                        except Exception:
                            pass
                        await _sleep(2)
                    if center_options:
                        logger.info(f"Mevcut merkezler: {center_options}")
                        center_rows = [[InlineKeyboardButton(c[:60], callback_data=f"vfs_pick:{c[:55]}")] for c in center_options]
                        center_text = await wait_input_func("🏢 *Başvuru merkezini seçin:*", center_rows)
                        if not center_text:
                            raise Exception("Merkez seçilmedi")
                        logger.info(f"Kullanıcı merkez seçti: {center_text!r}")
                        if notify_func:
                            await notify_func("⏳ *Vize kategorisi yükleniyor...*")
                elif center_text:
                    logger.info(f"Merkez önceden seçilmiş, Telegram sorusu atlanıyor: {center_text!r}")
    
                if not await select_mat_option(center_sel, center_text, "Merkez"):
                    # Center seçeneği hiç gelmedi — beklenmedik durum
                    await save_error_screenshot("center_option_yok")
                    raise Exception(f"Center seçeneği bulunamadı: {center_text!r}")
    
                # Center seçimi sonrası VFS API'nin kategori listesini yüklemesini bekle
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await _sleep(3)  # Angular'ın DOM'u güncellemesi için ek bekleme
                page_snippet = (await page.locator("body").inner_text())[:600]
                logger.info(f"Center sonrası sayfa: {page_snippet!r}")
    
                # VFS server hatası tespiti (502, 503, 500)
                page_lower = page_snippet.lower()
                if any(x in page_lower for x in ["502", "503", "500", "connectivity issue", "server error", "temporarily unavailable"]):
                    await save_error_screenshot("server_error_post_center")
                    logger.warning(f"VFS server hatası tespit edildi, sonraki hesap/proxy deneniyor")
                    return {
                        "ok": False,
                        "status_label": "ERROR",
                        "matched_text": "VFS geçici server hatası (5xx) - sonraki hesap deneniyor",
                        "signature": "server_error_5xx",
                    }
    
                try:
                    cat_html = await page.locator('mat-select[formcontrolname="visaCategoryCode"]').get_attribute("aria-disabled", timeout=5000) or "?"
                except Exception:
                    cat_html = "?"
                logger.info(f"Category mat-select aria-disabled={cat_html!r}")
    
                # Dropdown'daki mevcut seçenekleri çeken yardımcı fonksiyon
                async def get_mat_options(dropdown_sel: str, label: str) -> list[str]:
                    """Dropdown'ı açıp mevcut mat-option değerlerini döndürür."""
                    # Önce dropdown'ın enabled olmasını bekle (Angular API sonucunu bekler)
                    for _ in range(30):
                        try:
                            disabled = await page.locator(dropdown_sel).get_attribute("aria-disabled", timeout=3000)
                        except Exception:
                            disabled = None
                        if disabled != "true":
                            break
                        await _sleep(1)
    
                    # İlk olarak Angular bileşen options listesini DOM açmadan JS ile oku
                    # (VFS bazen mat-option'ları DOM'a yansıtmadan önce bileşen içinde tutar)
                    _js_direct = await page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return {opts: [], debug: 'no element'};
                        const ctx = el.__ngContext__;
                        const ctxType = typeof ctx;
                        const ctxIsArr = Array.isArray(ctx);
                        const items = ctx ? (Array.isArray(ctx) ? ctx : Object.values(ctx)) : [];
                        for (const c of items) {
                            if (!c || typeof c !== 'object') continue;
                            // MatSelect.options is a QueryList
                            const qList = c.options;
                            if (qList) {
                                const results = qList._results || (Array.isArray(qList) ? qList : []);
                                if (results.length > 0) {
                                    return {opts: results.map(o => String(o.viewValue || o.value || '')).filter(Boolean), debug: 'ok'};
                                }
                            }
                            // MatSelect component directly
                            if (typeof c.open === 'function' && c.options !== undefined) {
                                const ql = c.options;
                                const res = ql?._results || (Array.isArray(ql) ? ql : []);
                                if (res.length > 0) {
                                    return {opts: res.map(o => String(o.viewValue || o.value || '')).filter(Boolean), debug: 'matSelect.options'};
                                }
                            }
                        }
                        return {opts: [], debug: `ctxType=${ctxType} isArr=${ctxIsArr} items=${items.length}`};
                    }""", dropdown_sel)
                    _debug_info = _js_direct.get("debug", "?") if isinstance(_js_direct, dict) else str(_js_direct)
                    _js_opts = _js_direct.get("opts", []) if isinstance(_js_direct, dict) else (_js_direct if isinstance(_js_direct, list) else [])
                    logger.info(f"{label} JS direct read: {_debug_info}, opts={len(_js_opts)}")
                    if _js_opts:
                        logger.info(f"{label} JS Angular bileşen seçenekleri ({len(_js_opts)} adet): {_js_opts}")
                        return _js_opts
    
                    # Dropdown açmadan önce scroll et — görünür olması gerekiyor
                    try:
                        await page.locator(dropdown_sel).scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    await _sleep(1)
    
                    # Seçenek okuma için kullanılacak tüm selektörler
                    _opt_sels = [
                        "mat-option",
                        ".cdk-overlay-container mat-option",
                        ".mat-mdc-select-panel mat-option",
                        ".mat-mdc-option",
                        '[role="option"]',
                        ".mat-select-panel mat-option",
                        ".cdk-overlay-pane mat-option",
                        ".cdk-overlay-pane [role='option']",
                    ]
    
                    async def _try_open_and_read(click_method: str) -> list[str]:
                        """Belirtilen yöntemle dropdown'ı aç, seçenekleri oku."""
                        try:
                            if click_method == "trigger":
                                # MDC ve eski stil trigger'ları dene
                                trigger = page.locator(
                                    f"{dropdown_sel} .mat-mdc-select-trigger, "
                                    f"{dropdown_sel} .mat-select-trigger"
                                ).first
                                await trigger.click(timeout=8000)
                            elif click_method == "focus_space":
                                await page.locator(dropdown_sel).focus()
                                await page.keyboard.press("Space")
                            elif click_method == "js":
                                await page.evaluate("""(sel) => {
                                    const el = document.querySelector(sel);
                                    if (!el) return;
                                    const trigger = el.querySelector('.mat-select-trigger');
                                    if (trigger) { trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles:true})); trigger.click(); }
                                    else el.click();
                                    // Try MatSelect.open() directly
                                    const ctx = el.__ngContext__;
                                    const items = ctx ? (Array.isArray(ctx) ? ctx : Object.values(ctx)) : [];
                                    for (const c of items) {
                                        if (c && typeof c.open === 'function') { try { c.open(); } catch(e) {} break; }
                                    }
                                }""", dropdown_sel)
                            else:  # normal click
                                await page.locator(dropdown_sel).click(timeout=8000)
                        except Exception as _ce:
                            logger.debug(f"{label} open hata ({click_method}): {_ce}")
                            return []
    
                        # Panel / option görünene kadar bekle
                        for _pw in range(8):
                            await _sleep(1)
                            for _os in _opt_sels:
                                try:
                                    if await page.locator(_os).first.is_visible(timeout=500):
                                        opts = [t.strip() for t in await page.locator(_os).all_inner_texts() if t.strip()]
                                        if opts:
                                            return opts
                                except Exception:
                                    pass
                        # CDK overlay debug (ilk denemede logla)
                        try:
                            overlay_html = await page.evaluate("""() => {
                                const c = document.querySelector('.cdk-overlay-container');
                                return c ? c.innerHTML.substring(0, 500) : 'overlay yok';
                            }""")
                            logger.info(f"{label} CDK overlay ({click_method}): {overlay_html[:400]}")
                        except Exception:
                            pass
                        return []
    
                    click_methods = ["trigger", "click", "focus_space", "js"]
                    for _attempt in range(4):
                        method = click_methods[_attempt % len(click_methods)]
                        try:
                            opts = await _try_open_and_read(method)
                            await page.keyboard.press("Escape")
                            await _sleep(1)
                            if opts:
                                logger.info(f"{label} seçenekleri ({len(opts)} adet, yöntem={method}): {opts}")
                                return opts
                            logger.info(f"{label} get_mat_options deneme {_attempt+1} ({method}): boş")
                        except Exception as e:
                            logger.debug(f"{label} get_mat_options hata (deneme {_attempt+1}): {e}")
                        await _sleep(3)
                    return []
    
                # Doğru formcontrolname bilgileri (VFS Angular form analizi sonucu):
                # centerCode=merkez, selectedSubvisaCategory=kategori, visaCategoryCode=alt kategori
                # Tüm mat-select elementlerini enumerate et — doğru formcontrolname'i bul
                _all_mat_sels = await page.evaluate("""() => {
                    const sels = document.querySelectorAll('mat-select');
                    return Array.from(sels).map((s, i) => {
                        const ph = s.querySelector('.mat-mdc-select-placeholder, .mat-select-placeholder');
                        return {
                            idx: i,
                            fcn: s.getAttribute('formcontrolname'),
                            ngName: s.getAttribute('ng-reflect-name'),
                            ariaLabel: s.getAttribute('aria-label'),
                            placeholder: ph ? ph.textContent.trim() : '',
                            disabled: s.getAttribute('aria-disabled'),
                            id: s.id
                        };
                    });
                }""")
                logger.info(f"Tüm mat-select elementleri: {_all_mat_sels}")
    
                # Kategori dropdown'ını placeholder text ile bul
                # (formcontrolname="visaCategoryCode" yanlış elementi seçiyor olabilir)
                _cat_fcn = await page.evaluate("""() => {
                    const sels = document.querySelectorAll('mat-select');
                    // "appointment category" placeholder'ına göre bul
                    for (const s of sels) {
                        const ph = s.querySelector('.mat-mdc-select-placeholder, .mat-select-placeholder');
                        const txt = ph ? ph.textContent.toLowerCase() : '';
                        if (txt.includes('appointment category') || txt.includes('visa category') || txt.includes('your appointment category')) {
                            return {found: true, fcn: s.getAttribute('formcontrolname'), ngName: s.getAttribute('ng-reflect-name'), idx: Array.from(sels).indexOf(s)};
                        }
                    }
                    // Fallback: 2. mat-select (0=center, 1=category, 2=sub)
                    if (sels.length >= 2) {
                        const s = sels[1];
                        return {found: 'fallback-idx1', fcn: s.getAttribute('formcontrolname'), ngName: s.getAttribute('ng-reflect-name'), idx: 1};
                    }
                    return {found: false};
                }""")
                logger.info(f"Kategori mat-select bulucu: {_cat_fcn}")
    
                # cat_sel'i dinamik oluştur
                _cat_fcn_val = (_cat_fcn.get("fcn") or _cat_fcn.get("ngName")) if isinstance(_cat_fcn, dict) else None
                _cat_idx = _cat_fcn.get("idx", 1) if isinstance(_cat_fcn, dict) else 1
                if _cat_fcn_val:
                    cat_sel = f'mat-select[formcontrolname="{_cat_fcn_val}"]'
                else:
                    # Pozisyona göre nth-of-type fallback
                    cat_sel = f'mat-select:nth-of-type({_cat_idx + 1})'
                logger.info(f"cat_sel belirlendi: {cat_sel!r}")
    
                await wait_first_visible(page, [cat_sel, 'mat-select[formcontrolname="visaCategoryCode"]'], timeout_ms=20000)
                await _sleep(2)
    
                # === BOOKING MODUNDA: mevcut kategorileri kullanıcıya sor ===
                if booking_mode and wait_input_func:
                    cat_options = []
                    # Önce VFS API verisi (network intercept'ten anında gelir, en hızlı yol)
                    if _vfs_api_cats:
                        _name_key = next((k for k in ("name", "visaCategoryDesc", "description") if k in _vfs_api_cats[0]), None)
                        if _name_key:
                            cat_options = [item[_name_key] for item in _vfs_api_cats if item.get(_name_key)]
                            logger.info(f"Kategori VFS API'den alındı ({len(cat_options)} adet): {cat_options}")
                    # API verisi yoksa dropdown'ı aç ve oku
                    if not cat_options:
                        cat_options = await get_mat_options(cat_sel, "Kategori")
                    if not cat_options:
                        _sj_cats = site.get("vfs_options", {}).get("categories", [])
                        if _sj_cats:
                            logger.info(f"Kategori dropdown boş, sites.json fallback: {_sj_cats}")
                            cat_options = _sj_cats
                    if not cat_options:
                        # Bu merkez için kategori yok — kullanıcıya bildir ve başka merkez seçtir
                        if notify_func:
                            await notify_func(f"⚠️ *{center_text}* için randevu kategorisi bulunamadı (merkez kapalı olabilir).\nBaşka bir merkez seçebilirsiniz.")
                        retry_center_opts = []
                        for _rc in range(3):
                            try:
                                await page.locator(center_sel).click(timeout=10000)
                                try:
                                    await page.wait_for_selector("mat-option", state="visible", timeout=10000)
                                except Exception:
                                    pass
                                retry_center_opts = [t.strip() for t in await page.locator("mat-option").all_inner_texts() if t.strip()]
                                await page.keyboard.press("Escape")
                                if retry_center_opts:
                                    break
                            except Exception:
                                pass
                            await _sleep(2)
                        if retry_center_opts:
                            retry_rows = [[InlineKeyboardButton(c[:60], callback_data=f"vfs_pick:{c[:55]}")] for c in retry_center_opts]
                            retry_rows.append([InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")])
                            new_center = await wait_input_func("🏢 *Başka bir merkez seçin:*", retry_rows)
                            if new_center and new_center != "__cancel__":
                                center_text = new_center
                                logger.info(f"Kullanıcı yeni merkez seçti: {center_text!r}")
                                if await select_mat_option(center_sel, center_text, "Merkez"):
                                    await _sleep(5)
                                    cat_options = await get_mat_options(cat_sel, "Kategori")
                                    logger.info(f"Yeni merkez kategori seçenekleri: {cat_options}")
                        if not cat_options:
                            _nocat_ss = f"/tmp/vfs_nocat_{int(time.time())}.png"
                            try:
                                await page.screenshot(path=_nocat_ss, full_page=True)
                                await send_admin_photo(_nocat_ss, f"⚠️ Kategori bulunamadı: {center_text}")
                            except Exception:
                                pass
                            return {
                                "ok": False, "status_label": "ERROR",
                                "matched_text": f"{center_text} için kategori yok",
                                "signature": "nocat_error",
                            }
                    if category_text:
                        logger.info(f"Kategori önceden seçilmiş, atlanıyor: {category_text!r}")
                        if notify_func:
                            await notify_func(f"✅ Kategori: *{category_text}* _(kayıttan)_")
                    elif len(cat_options) == 1:
                        category_text = cat_options[0]
                        logger.info(f"Kategori tek seçenek, otomatik seçildi: {category_text!r}")
                        if notify_func:
                            await notify_func(f"✅ Kategori otomatik seçildi: *{category_text}*")
                    else:
                        cat_rows = [[InlineKeyboardButton(c[:60], callback_data=f"vfs_pick:{c[:55]}")] for c in cat_options]
                        category_text = await wait_input_func("📋 *Vize kategorisini seçin:*", cat_rows)
                        if not category_text:
                            raise Exception("Kategori seçilmedi")
                        logger.info(f"Kullanıcı kategori seçti: {category_text!r}")
                    if notify_func:
                        await notify_func("⏳ *Alt kategoriler yükleniyor...*")
                    # Kategori kodu ve merkez kodunu bul → subcategory API'yi direkt çek
                    _cat_code_api = None
                    _center_code_api = None
                    for _item in _vfs_api_cats:
                        _n = _item.get("name") or _item.get("visaCategoryDesc") or ""
                        _c = _item.get("code") or _item.get("visaCategoryCode") or ""
                        if _n and _c and (category_text.lower() in _n.lower() or _n.lower() in category_text.lower()):
                            _cat_code_api = _c
                            _center_code_api = _item.get("centerCode") or _item.get("center_code") or ""
                            break
                    if not _center_code_api and _vfs_api_cats:
                        _center_code_api = _vfs_api_cats[0].get("centerCode") or _vfs_api_cats[0].get("center_code") or ""
                # Kategori seçiminden önce _vfs_api_subcats'ı temizle — yeni kategori için taze veri bekliyoruz
                _subcats_before_clear = list(_vfs_api_subcats)  # eski veriyi sakla
                _vfs_api_subcats.clear()
    
                _cat_ctrl_name = _cat_fcn_val or "visaCategoryCode"
                cat_found = await _select_with_js_fallback(cat_sel, _cat_ctrl_name, category_text, "Kategori", _vfs_api_cats)
                if not cat_found:
                    logger.warning(f"Kategori DOM/JS seçimi başarısız: {category_text!r} — yine de devam ediliyor")
    
                # Angular'ın subvisacategory API'sini tetikleyip tetiklemediğini bekle (max 15s)
                for _sw in range(15):
                    if _vfs_api_subcats:
                        logger.info(f"subvisacategory network'ten geldi ({_sw+1}s): {len(_vfs_api_subcats)} adet")
                        break
                    await _sleep(1)
    
                # Network isteği gelmedi ama önceden yakalanmış veri varsa geri yükle
                if not _vfs_api_subcats and _subcats_before_clear:
                    logger.info(f"subvisacategory network'ten gelmedi — önceden yakalanan {len(_subcats_before_clear)} adet veri kullanılıyor")
                    _vfs_api_subcats.extend(_subcats_before_clear)
    
                # Hâlâ boşsa doğrudan API çağır
                if not _vfs_api_subcats and _cat_code_api and _center_code_api:
                    logger.info("subvisacategory network'ten gelmedi, direkt API deneniyor...")
                    await _fetch_subcats_direct(_center_code_api, _cat_code_api)
    
                # Subcategory dropdown'ın populate olmasını bekle (Angular API'den dolması için)
                sub_sel = 'mat-select[formcontrolname="visaCategoryCode"]'
                await wait_first_visible(page, [sub_sel], timeout_ms=20000)
                await _sleep(2)
    
                # === BOOKING MODUNDA: mevcut alt kategorileri kullanıcıya sor ===
                if booking_mode and wait_input_func:
                    sub_options = []
                    # API verisi zaten varsa DOM okumayı atla — Angular state sorunu nedeniyle DOM boş kalır
                    if _vfs_api_subcats:
                        logger.info(f"Alt kategori API item[0] değerler: { {k: _vfs_api_subcats[0].get(k) for k in ('name','description','subCategoryDesc','visaSubCategoryDesc','code')} }")
                        _desc_key = next((k for k in ("subCategoryDesc", "visaSubCategoryDesc", "description", "name") if _vfs_api_subcats[0].get(k)), None)
                        if _desc_key:
                            sub_options = [item[_desc_key] for item in _vfs_api_subcats if item.get(_desc_key)]
                            logger.info(f"Alt kategori VFS API'den alındı ({len(sub_options)} adet): {sub_options}")
                    # API verisi yoksa DOM'dan oku (yavaş ama fallback)
                    if not sub_options:
                        sub_options = await get_mat_options(sub_sel, "Alt Kategori")
                        if sub_options:
                            logger.info(f"Alt kategori DOM'dan alındı ({len(sub_options)} adet): {sub_options}")
                    if not sub_options:
                        _cat_sub_map = site.get("vfs_options", {}).get("category_subcategory_map", {})
                        _sj_subs = _cat_sub_map.get(category_text) or site.get("vfs_options", {}).get("subcategories", [])
                        if _sj_subs:
                            logger.info(f"Alt kategori dropdown boş, sites.json fallback: {_sj_subs}")
                            sub_options = _sj_subs
                    if not sub_options:
                        _nosub_ss = f"/tmp/vfs_nosub_{int(time.time())}.png"
                        try:
                            await page.screenshot(path=_nosub_ss, full_page=True)
                            await send_admin_photo(_nosub_ss, f"⚠️ Alt kategori bulunamadı: {center_text} / {category_text}")
                        except Exception:
                            pass
                        if notify_func:
                            await notify_func(f"⚠️ *{category_text}* için alt kategori bulunamadı — teknik bir sorun olabilir.")
                        return {
                            "ok": False, "status_label": "ERROR",
                            "matched_text": f"{category_text} için alt kategori yok",
                            "signature": "nosub_error",
                        }
                    # Tek seçenek varsa otomatik seç, kullanıcıya bildir
                    if len(sub_options) == 1:
                        subcategory_text = sub_options[0]
                        logger.info(f"Alt kategori tek seçenek, otomatik seçildi: {subcategory_text!r}")
                        if notify_func:
                            await notify_func(f"✅ Alt kategori otomatik seçildi: *{subcategory_text}*")
                    elif subcategory_text:
                        logger.info(f"Alt kategori önceden seçilmiş, atlanıyor: {subcategory_text!r}")
                        if notify_func:
                            await notify_func(f"✅ Alt kategori: *{subcategory_text}* _(kayıttan)_")
                    else:
                        sub_rows = [[InlineKeyboardButton(s[:60], callback_data=f"vfs_pick:{s[:55]}")] for s in sub_options]
                        subcategory_text = await wait_input_func("📄 *Alt kategoriyi seçin:*", sub_rows)
                    if not subcategory_text:
                        raise Exception("Alt kategori seçilmedi")
                    logger.info(f"Kullanıcı alt kategori seçti: {subcategory_text!r}")
    
                # Alt kategori kodunu API verisinden çıkar (CheckIsSlotAvailable için gerekli)
                _subcat_code_api = None
                for _si in _vfs_api_subcats:
                    _sn = _si.get("name") or _si.get("subCategoryDesc") or _si.get("description") or ""
                    _sc = _si.get("code") or _si.get("subCategoryCode") or ""
                    if _sn and _sc and (subcategory_text.lower() in _sn.lower() or _sn.lower() in subcategory_text.lower()):
                        _subcat_code_api = _sc
                        logger.info(f"Alt kategori API kodu: {_sn!r} → {_sc!r}")
                        break
                if not _subcat_code_api and _vfs_api_subcats:
                    _subcat_code_api = (_vfs_api_subcats[0].get("code") or _vfs_api_subcats[0].get("subCategoryCode") or "")
                    logger.warning(f"Alt kategori tam eşleşme yok, ilk kodu kullanıyorum: {_subcat_code_api!r}")
    
                sub_found = await _select_with_js_fallback(sub_sel, "visaCategoryCode", subcategory_text, "Alt kategori", _vfs_api_subcats)
                if not sub_found:
                    logger.warning(f"Alt kategori DOM/JS seçimi başarısız: {subcategory_text!r} — yine de devam ediliyor")
    
                await _sleep(2)
    
                # === CheckIsSlotAvailable API — doğrudan slot kontrolü ===
                async def _check_slot_available_direct() -> dict | None:
                    """VFS CheckIsSlotAvailable API'sini doğrudan çağır (Angular form bypass)."""
                    try:
                        center_code = _center_code_api or (_vfs_api_cats[0].get("centerCode") or "") if _vfs_api_cats else ""
                        subcat_code = _subcat_code_api or ""
                        if not center_code or not subcat_code:
                            logger.warning(f"CheckIsSlotAvailable için eksik veri: center={center_code!r} subcat={subcat_code!r}")
                            return None
                        import re as _re2
                        _url2 = site.get("url", "")
                        _m2 = _re2.search(r'vfsglobal\.com/([a-z]+)/([a-z-]+)/([a-z]+)/', _url2)
                        if _m2:
                            _mc, _lc, _cc = _m2.group(1), _m2.group(2), _m2.group(3)
                        else:
                            _mc, _cc = "tur", "fra"
                        _login = (cfg or {}).get("email") or site.get("email") or ""
                        payload = {
                            "countryCode": _mc, "missionCode": _cc, "vacCode": center_code,
                            "visaCategoryCode": subcat_code, "roleName": "Individual",
                            "loginUser": _login, "payCode": "",
                        }
                        api_url = "https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable"
                        extra_h = {"content-type": "application/json;charset=UTF-8", "accept": "application/json, text/plain, */*"}
                        if _vfs_api_headers.get("clientsource"):
                            extra_h["clientsource"] = _vfs_api_headers["clientsource"]
                        if _vfs_api_headers.get("authorize"):
                            extra_h["authorize"] = _vfs_api_headers["authorize"]
                        if _vfs_api_headers.get("route"):
                            extra_h["route"] = _vfs_api_headers["route"]
                        logger.info(f"CheckIsSlotAvailable direkt: center={center_code} subcat={subcat_code} login={_login}")
                        resp = await page.request.post(api_url, data=json.dumps(payload), headers=extra_h)
                        if resp.status == 200:
                            data = await resp.json()
                            logger.info(f"CheckIsSlotAvailable sonuç: {data}")
                            return data
                        logger.warning(f"CheckIsSlotAvailable HTTP {resp.status}")
                    except Exception as _ce:
                        logger.warning(f"CheckIsSlotAvailable direkt hata: {_ce}")
                    return None
    
                # === Continue tıkla: appointment-detail sayfasından slot/tarih sayfasına geç ===
                if notify_func:
                    await notify_func("⏳ Uygun randevu tarihleri kontrol ediliyor...")
    
                cont_clicked = False
                for _ca in range(15):
                    try:
                        cont_btn = page.locator(
                            'button:has-text("Continue"), button[type="submit"]'
                        ).first
                        if await cont_btn.is_visible(timeout=2000):
                            disabled = await cont_btn.get_attribute("disabled", timeout=2000)
                            aria_dis = await cont_btn.get_attribute("aria-disabled", timeout=2000)
                            if disabled is None and aria_dis not in ("true", "True"):
                                await cont_btn.click()
                                logger.info("Appointment-detail Continue tıklandı")
                                cont_clicked = True
                                break
                            # Disabled ama son 3 denemede force-click dene
                            if _ca >= 12:
                                logger.info(f"Continue disabled ({disabled=}, {aria_dis=}) — JS force-click deneniyor")
                                _fc = await page.evaluate("""() => {
                                    const btns = [...document.querySelectorAll('button')];
                                    const b = btns.find(b => b.textContent.trim().toLowerCase().includes('continue') || b.type === 'submit');
                                    if (!b) return 'not found';
                                    b.removeAttribute('disabled');
                                    b.removeAttribute('aria-disabled');
                                    b.click();
                                    return b.textContent.trim();
                                }""")
                                logger.info(f"Continue JS force-click sonucu: {_fc!r}")
                                if _fc and _fc != 'not found':
                                    cont_clicked = True
                                    break
                    except Exception:
                        pass
                    await _sleep(2)
    
                if cont_clicked:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    await _sleep(3)
                    # Hangi sayfaya geçildiğini tespit et
                    _cont_url = page.url
                    _cont_body = await page.locator("body").inner_text()
                    _cont_lower = _cont_body.lower()
                    logger.info(f"Continue sonrası URL: {_cont_url}, sayfa (250): {_cont_body[:250]!r}")
                    # "Your Details" (kişisel bilgiler) sayfasına geçildiyse booking akışını başlat
                    _on_your_details = any(x in _cont_lower for x in [
                        "your details", "first name", "last name", "date of birth", "passport number",
                        "kişisel bilgi", "ad soyad", "doğum tarihi", "pasaport"
                    ])
                    if _on_your_details:
                        logger.info("'Your Details' sayfası tespit edildi — booking akışı başlatılıyor")
                        if notify_func:
                            await notify_func("✅ Randevu detayları onaylandı! Kişisel bilgiler sayfasına geçildi.")
                        # Ekran görüntüsü al
                        _ss_path = f"/tmp/vfs_your_details_{int(time.time())}.png"
                        try:
                            await page.screenshot(path=_ss_path, full_page=True)
                            logger.info(f"Your Details ekran görüntüsü alındı: {_ss_path}")
                            await send_admin_photo(_ss_path, f"📸 Kişisel bilgiler sayfası: {_ss_path}")
                        except Exception as _sse:
                            logger.warning(f"Screenshot hatası: {_sse}")
                        if booking_mode and wait_input_func:
                            return await _proceed_booking()
                        return {
                            "ok": True, "status_label": "OPEN",
                            "matched_text": f"{center_text} — Kişisel bilgiler sayfasına geçildi",
                            "signature": hashlib.sha256(f"vfs_your_details:{center_text}".encode()).hexdigest()[:16],
                        }
                else:
                    logger.warning("Continue butonu tıklanamadı — direkt API çağrılıyor")
                    # Direkt API ile slot kontrolü (Angular form bypass)
                    _slot_result = await _check_slot_available_direct()
                    if _slot_result is not None:
                        _slot_error = _slot_result.get("error")
                        _earliest = _slot_result.get("earliestDate") or ""
                        if _slot_error:
                            logger.info(f"API: slot yok — error: {_slot_error}")
                            return {
                                "ok": True, "status_label": "CLOSED",
                                "matched_text": f"{center_text} — {subcategory_text}: Slot yok (API)",
                                "signature": hashlib.sha256(f"vfs_closed_api:{center_text}:{subcategory_text}".encode()).hexdigest()[:16],
                            }
                        if _earliest:
                            # Tarih formatını temizle: "03/23/2026 00:00:00" → "23/03/2026"
                            import re as _re3
                            _dm = _re3.search(r'(\d{2})/(\d{2})/(\d{4})', _earliest)
                            _found_date = f"{_dm.group(2)}/{_dm.group(1)}/{_dm.group(3)}" if _dm else _earliest[:10]
                            logger.info(f"API: slot bulundu! En erken: {_found_date}")
                            if booking_mode and wait_input_func:
                                confirm_rows = [
                                    [InlineKeyboardButton("✅ Randevu Oluştur", callback_data="vfs_pick:__confirm_book__")],
                                    [InlineKeyboardButton("🔄 Başka Merkez Kontrol Et", callback_data="vfs_pick:__retry_center__")],
                                    [InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")],
                                ]
                                _slot_list = _slot_result.get("earliestSlotLists") or []
                                _slot_info = "\n".join([f"  {s.get('applicant','?')} başvurucu: {s.get('date','?')[:10]}" for s in _slot_list[:5]])
                                confirm = await wait_input_func(
                                    f"📅 *Randevu müsait!*\n\n"
                                    f"📍 {center_text}\n"
                                    f"📋 {category_text} / {subcategory_text}\n"
                                    f"📆 En erken: *{_found_date}*\n"
                                    f"{_slot_info}\n\n"
                                    f"Randevuyu oluşturmak istiyor musunuz?",
                                    confirm_rows,
                                )
                                if confirm == "__confirm_book__":
                                    # Kullanıcı onayladı — Continue butonunu JS ile force-click dene
                                    logger.info("Kullanıcı randevu onayladı — Continue force-click deneniyor...")
                                    if notify_func:
                                        await notify_func("🔄 Randevu sayfasına geçiliyor...")
                                    _force_cont = False
                                    try:
                                        _fres = await page.evaluate("""() => {
                                            const btns = Array.from(document.querySelectorAll('button'));
                                            for (const b of btns) {
                                                const t = b.textContent.trim().toLowerCase();
                                                if (t === 'continue' || t === 'save' || b.type === 'submit') {
                                                    b.removeAttribute('disabled');
                                                    b.removeAttribute('aria-disabled');
                                                    b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                                    return 'clicked:' + b.textContent.trim();
                                                }
                                            }
                                            return 'not-found';
                                        }""")
                                        logger.info(f"Force Continue sonuç: {_fres}")
                                        if _fres and _fres != 'not-found':
                                            _force_cont = True
                                            try:
                                                await page.wait_for_load_state("networkidle", timeout=15000)
                                            except Exception:
                                                pass
                                            await _sleep(3)
                                    except Exception as _fce:
                                        logger.warning(f"Force Continue hatası: {_fce}")
                                    # Sayfanın gerçekten geçip geçmediğini kontrol et
                                    _post_url = page.url
                                    try:
                                        _post_body = await asyncio.wait_for(
                                            page.locator("body").inner_text(), timeout=10.0
                                        )
                                        _post_lower = _post_body.lower()
                                    except Exception:
                                        _post_lower = ""
                                    _on_your_details_now = any(x in _post_lower for x in [
                                        "your details", "first name", "last name",
                                        "date of birth", "passport number", "dob"
                                    ])
                                    # "choose your application centre" sadece appointment-detail sayfasında olur
                                    # "sub-category" ve "appointment details" sonraki sayfanın recap bölümünde görünebilir
                                    _still_on_appt_now = (
                                        "choose your application centre" in _post_lower or
                                        "choose your centre" in _post_lower
                                    )
                                    # URL değişmesi en güvenilir sinyal
                                    _url_left_appt = "appointment-detail" not in _post_url.lower()
                                    # Sayfa geçti mi? URL değişti VEYA kişisel bilgiler görünüyor (ve appt sayfasında değil)
                                    _page_advanced = _url_left_appt or (_on_your_details_now and not _still_on_appt_now)
                                    logger.info(f"Onay sonrası URL: {_post_url}, url_left={_url_left_appt}, your_details={_on_your_details_now} still_appt={_still_on_appt_now} → advanced={_page_advanced}")
                                    if not _page_advanced:
                                        # Continue geçmedi — kullanıcıya bildir, akışı durdur
                                        logger.warning("Continue onay sonrası geçmedi — booking iptal")
                                        if notify_func:
                                            await notify_func(
                                                "⚠️ *Continue butonu geçemedi* — kişisel bilgiler sayfasına ulaşılamadı.\n\n"
                                                "Bu genellikle VFS'in Angular formu geciktirmesinden kaynaklanır. "
                                                "Lütfen birkaç dakika bekleyip tekrar deneyin veya farklı bir merkez seçin."
                                            )
                                        return {"ok": False, "status_label": "ERROR",
                                                "matched_text": "Continue sayfası geçilemedi", "signature": "continue_nav_fail"}
                                    # Ekran görüntüsü al
                                    _ss_path = f"/tmp/vfs_booking_after_confirm_{int(time.time())}.png"
                                    try:
                                        await asyncio.wait_for(
                                            page.screenshot(path=_ss_path, full_page=True), timeout=10.0
                                        )
                                        logger.info(f"Booking onay sonrası ekran görüntüsü: {_ss_path}")
                                        await send_admin_photo(_ss_path, f"📸 Randevu onay sayfası: {_ss_path}")
                                    except Exception as _sse:
                                        logger.warning(f"Screenshot hatası: {_sse}")
                                    # Booking akışına devam et
                                    return await _proceed_booking()
                                if confirm == "__retry_center__":
                                    if notify_func:
                                        await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                                    try:
                                        await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                                    except Exception:
                                        pass
                                    await _sleep(3)
                                    _sel_center = None
                                    _sel_category = None
                                    _sel_subcategory = None
                                    continue
                                return {
                                    "ok": True, "status_label": "CANCELLED",
                                    "matched_text": "Kullanıcı iptal etti",
                                    "signature": "user_cancel",
                                }
                            return {
                                "ok": True, "status_label": "OPEN",
                                "matched_text": f"{center_text} — {_found_date} (API doğrudan)",
                                "signature": hashlib.sha256(f"vfs_open_api:{center_text}:{_found_date}".encode()).hexdigest()[:16],
                            }
    
                final_text = await page.locator("body").inner_text()
                final_lower = final_text.lower()
                logger.info(f"Continue sonrası sayfa (400): {final_text[:400]!r}")
    
                date_match = re.search(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b", final_text)
    
                pending_slots = re.findall(
                    r'earliest available slot for (\d+) applicants? is\s*:\s*(\d{2}[/-]\d{2}[/-]\d{4})',
                    final_lower
                )
                if pending_slots:
                    slot_lines = "\n".join(f"{n} kişi: {d}" for n, d in pending_slots)
                    if booking_mode and wait_input_func:
                        # Kullanıcıya en yakın tarihi bildir, onay iste
                        slot_msg = "\n".join(f"👥 {n} kişi için: *{d}*" for n, d in pending_slots)
                        if notify_func:
                            await notify_func(f"📅 *En yakın müsait tarihler:*\n{slot_msg}")
                        confirm_rows = [
                            [InlineKeyboardButton("✅ Randevu Oluştur", callback_data="vfs_pick:__confirm_book__")],
                            [InlineKeyboardButton("🔄 Başka Merkez Kontrol Et", callback_data="vfs_pick:__retry_center__")],
                            [InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")],
                        ]
                        confirm = await wait_input_func("Randevuyu oluşturmak istiyor musunuz?", confirm_rows)
                        if confirm == "__confirm_book__":
                            return await _proceed_booking()
                        if confirm == "__retry_center__":
                            if notify_func:
                                await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                            try:
                                await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass
                            await _sleep(3)
                            _sel_center = None
                            _sel_category = None
                            _sel_subcategory = None
                            continue
                        return {"ok": True, "status_label": "CANCELLED", "matched_text": "Kullanıcı iptal etti",
                                "signature": "user_cancel"}
                    return {
                        "ok": True,
                        "status_label": "PENDING",
                        "matched_text": slot_lines,
                        "signature": hashlib.sha256(
                            f"vfs_pending:{center_text}:{slot_lines}".encode()
                        ).hexdigest()[:16],
                    }
    
                if any(x in final_lower for x in ["earliest available slot", "available slot", "select date", "select appointment date", "calendar"]):
                    found_date = date_match.group(1) if date_match else "-"
                    if booking_mode and wait_input_func:
                        if notify_func:
                            await notify_func(f"📅 *Randevu tarihi mevcut!* En yakın: *{found_date}*")
                        confirm_rows = [
                            [InlineKeyboardButton("✅ Randevu Oluştur", callback_data="vfs_pick:__confirm_book__")],
                            [InlineKeyboardButton("🔄 Başka Merkez Kontrol Et", callback_data="vfs_pick:__retry_center__")],
                            [InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")],
                        ]
                        confirm = await wait_input_func("Randevuyu oluşturmak istiyor musunuz?", confirm_rows)
                        if confirm == "__confirm_book__":
                            return await _proceed_booking()
                        if confirm == "__retry_center__":
                            if notify_func:
                                await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                            try:
                                await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass
                            await _sleep(3)
                            _sel_center = None
                            _sel_category = None
                            _sel_subcategory = None
                            continue
                        return {"ok": True, "status_label": "CANCELLED", "matched_text": "Kullanıcı iptal etti",
                                "signature": "user_cancel"}
                    return {
                        "ok": True,
                        "status_label": "OPEN",
                        "matched_text": found_date,
                        "signature": hashlib.sha256(
                            f"vfs_open:{center_text}:{category_text}:{subcategory_text}:{found_date}".encode()
                        ).hexdigest()[:16],
                    }
    
                if any(x in final_lower for x in ["no slots available", "currently unavailable", "no appointment", "please try again later", "no appointment slots are currently available"]):
                    if notify_func:
                        await notify_func(f"😔 *{center_text}* — *{category_text}* için şu an uygun randevu tarihi bulunmuyor.")
                    if await _ask_retry_center(
                        f"😔 *{center_text} / {category_text}* için uygun randevu tarihi yok.\n\nBaşka bir merkez kontrol etmek ister misiniz?"
                    ):
                        if notify_func:
                            await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                        try:
                            await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        await _sleep(3)
                        _sel_center = None
                        _sel_category = None
                        _sel_subcategory = None
                        continue
                    return {
                        "ok": True,
                        "status_label": "CLOSED",
                        "matched_text": f"{center_text} — {category_text}: Slot yok",
                        "signature": hashlib.sha256(f"vfs_closed_noslot:{center_text}:{category_text}".encode()).hexdigest()[:16],
                    }
    
                if date_match:
                    if booking_mode and wait_input_func:
                        if notify_func:
                            await notify_func(f"📅 *Randevu tarihi bulundu:* *{date_match.group(1)}*")
                        confirm_rows = [
                            [InlineKeyboardButton("✅ Randevu Oluştur", callback_data="vfs_pick:__confirm_book__")],
                            [InlineKeyboardButton("🔄 Başka Merkez Kontrol Et", callback_data="vfs_pick:__retry_center__")],
                            [InlineKeyboardButton("❌ İptal", callback_data="vfs_pick:__cancel__")],
                        ]
                        confirm = await wait_input_func("Randevuyu oluşturmak istiyor musunuz?", confirm_rows)
                        if confirm == "__confirm_book__":
                            return await _proceed_booking()
                        if confirm == "__retry_center__":
                            if notify_func:
                                await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                            try:
                                await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                pass
                            await _sleep(3)
                            _sel_center = None
                            _sel_category = None
                            _sel_subcategory = None
                            continue
                        return {"ok": True, "status_label": "CANCELLED", "matched_text": "Kullanıcı iptal etti",
                                "signature": "user_cancel"}
                    return {
                        "ok": True,
                        "status_label": "OPEN",
                        "matched_text": date_match.group(1),
                        "signature": hashlib.sha256(f"vfs_open_date:{date_match.group(1)}".encode()).hexdigest()[:16],
                    }
    
                await save_error_screenshot("vfs_belirsiz")
                logger.info(f"Belirsiz durum — sayfa içeriği: {final_text[:500]!r}")
                if notify_func:
                    await notify_func(f"😔 *{center_text}* için uygun randevu tarihi bulunamadı.")
                if await _ask_retry_center(
                    f"😔 *{center_text} / {category_text}* için uygun randevu tarihi bulunamadı.\n\nBaşka bir merkez kontrol etmek ister misiniz?"
                ):
                    if notify_func:
                        await notify_func("⏳ Başvuru sayfasına geri dönülüyor...")
                    try:
                        await page.goto(_appt_detail_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    await _sleep(3)
                    _sel_center = None
                    _sel_category = None
                    _sel_subcategory = None
                    continue
                return {
                    "ok": True,
                    "status_label": "CLOSED",
                    "matched_text": f"{center_text} — uygun tarih bulunamadı",
                    "signature": hashlib.sha256(f"vfs_closed_belirsiz:{center_text}:{category_text}".encode()).hexdigest()[:16],
                }

        except PlaywrightTimeoutError as exc:
            await save_error_screenshot("timeout")
            return {
                "ok": False,
                "status_label": "ERROR",
                "matched_text": f"Timeout: {str(exc)[:100]}",
                "signature": "timeout",
            }
        except Exception as exc:
            if "queue_it_max_retries" in str(exc):
                logger.warning("Queue-it max retry — rate_limited döndürülüyor")
                return {
                    "ok": False,
                    "status_label": "ERROR",
                    "matched_text": "Queue-it bekleme kuyruğu — maksimum deneme aşıldı",
                    "signature": "rate_limited",
                }
            await save_error_screenshot("genel_hata")
            logger.exception("Genel hata")
            return {
                "ok": False,
                "status_label": "ERROR",
                "matched_text": f"Hata: {str(exc)[:120]}",
                "signature": "error",
            }

    # VFS: Camoufox (Firefox tabanlı, Cloudflare Turnstile'ı geçer)
    # Account rotation + proxy rotation destekler
    if provider == "VFS":
        # Kullanıcı Telegram'dan kendi bilgilerini girdiyse sadece onu kullan
        _dyn_email = (dynamic_config or {}).get("email", "").strip()
        _dyn_pass  = (dynamic_config or {}).get("password", "").strip()
        if _dyn_email and _dyn_pass:
            account_pool = [{
                "email":         _dyn_email,
                "password":      _dyn_pass,
                "imap_password": (dynamic_config or {}).get("imap_password", ""),
            }]
        else:
            # Otomasyon modu: dynamic_config olmadan hesap yok → erken hata
            logger.warning(f"VFS için e-posta/şifre girilmemiş, site: {site.get('name','?')}")
            return {"ok": False, "status_label": "ERROR", "matched_text": "VFS hesap bilgisi girilmemiş — müşteri kaydına VFS e-posta/şifre ekleyin", "signature": "no_credentials"}

        # Global proxy pool: optimizer'ın başarı istatistiğine göre sırala
        # (Hesaba özel proxy olan hesaplar bu havuzu kullanmaz)
        proxy_pool = []
        if proxies and use_proxy:
            proxy_pool = rank_proxies(list(proxies))
        if not proxy_pool:
            proxy_pool = [None]

        # Account pool: cooldown'da olanları sona at
        account_pool = rank_accounts(account_pool)

        last_result = {"ok": False, "status_label": "ERROR", "matched_text": "Tüm hesap/proxy kombinasyonları başarısız", "signature": "all_failed"}

        def _is_proxy_network_error(res: dict) -> bool:
            sig = res.get("signature", "")
            text = res.get("matched_text", "").lower()
            if sig in ("proxy_liftassets_failed", "proxy_error"):
                return True
            if sig == "error" and any(x in text for x in (
                "unknown error", "liftassets_proxy_fail", "proxy", "connection",
            )):
                return True
            return False

        def _is_rate_limited(res: dict) -> bool:
            return res.get("signature") in ("rate_limited", "server_error_5xx")

        def _is_account_restricted(res: dict) -> bool:
            return res.get("signature") == "account_restricted"

        def _is_session_expired(res: dict) -> bool:
            return res.get("signature") == "session_expired"

        # Turnstile süresini _run_page içinden dış scope'a taşımak için paylaşılan liste
        _cf_elapsed: list[float] = [0.0]

        for acct_idx, try_account in enumerate(account_pool):
            acct_email = try_account["email"]

            # Cooldown kontrolü (rate-limit/restrict)
            remaining = get_cooldown_remaining(acct_email)
            if remaining > 0:
                logger.info(f"Hesap soğumada, atlanıyor ({remaining}s): {acct_email}")
                continue

            # Minimum kullanım aralığı kontrolü — aynı hesap çok sık kullanılmasın
            _last_used = _account_last_used.get(acct_email, 0.0)
            _cpf_check = _cookies_path(acct_email)
            _has_session = _cpf_check.exists() and (time.time() - _cpf_check.stat().st_mtime) < 14400
            _min_interval = _ACCOUNT_MIN_INTERVAL_SESSION if _has_session else _ACCOUNT_MIN_INTERVAL_LOGIN
            _since_last = time.time() - _last_used
            if _since_last < _min_interval:
                logger.info(f"Hesap minimum interval bekleniyor ({_since_last:.0f}s < {_min_interval}s): {acct_email}")
                continue

            # Aktif hesap bilgilerini güncelle (closure üzerinden _run_page okur)
            _active_account["email"] = acct_email
            _active_account["password"] = try_account["password"]
            _active_account["imap_password"] = try_account["imap_password"]
            logger.info(f"Hesap deneniyor ({acct_idx+1}/{len(account_pool)}): {acct_email}")

            # Hesaba özel proxy tanımlıysa sadece onu kullan, yoksa global havuz
            _acct_proxy_str = try_account.get("proxy", "").strip()
            _ip_reset_url = try_account.get("ip_reset_url", "").strip() if _acct_proxy_str else ""
            # Hesaba özel ip_reset_url yoksa proxy_rotation.json'dan global URL'yi al
            if not _ip_reset_url:
                try:
                    _rot_file = DATA_DIR / "proxy_rotation.json"
                    if _rot_file.exists():
                        _rot_data = json.loads(_rot_file.read_text(encoding="utf-8"))
                        if _rot_data:
                            _ip_reset_url = list(_rot_data.values())[0].get("url", "")
                except Exception:
                    pass
            if _acct_proxy_str and use_proxy:
                _acct_proxy = _parse_proxy_line(_acct_proxy_str)
                # ip_reset_url varsa aynı proxy ile bir reset denemesi için listeye iki kez ekle
                _this_proxy_pool = [_acct_proxy, _acct_proxy] if _ip_reset_url else [_acct_proxy]
                logger.info(f"Hesaba özel proxy: {_acct_proxy.get('server') if _acct_proxy else 'None'}")
            else:
                # Global proxy havuzu — ip_reset_url varsa her proxy için bir retry şansı
                _this_proxy_pool = (proxy_pool + proxy_pool) if (_ip_reset_url and proxy_pool != [None]) else proxy_pool
            _ip_reset_done = False  # Bu hesap için IP reset bir kez yapılabilir

            for try_proxy in _this_proxy_pool:
                # SOCKS5 tüneli ölmüşse yeniden aç
                try_proxy = _refresh_socks5_proxy(try_proxy) if try_proxy else try_proxy
                proxy_server = try_proxy.get("server") if try_proxy else "direct"
                logger.info(f"Proxy deneniyor: {proxy_server}")
                _attempt_start = time.time()
                _vw, _vh = random.choice(_VIEWPORT_POOL)
                camoufox_kwargs: dict = {
                    "headless": True,
                    # OS randomizasyonu: %70 Windows, %30 macOS — gerçek kullanıcı dağılımına yakın
                    "os": random.choices(["windows", "macos"], weights=[70, 30])[0],
                    "locale": random.choice([
                        ["tr-TR", "tr", "en-US"],
                        ["tr-TR", "tr", "en-GB"],
                        ["tr-TR", "en-US", "tr"],
                    ]),
                    "geoip": True,
                }
                if try_proxy:
                    camoufox_kwargs["proxy"] = {k: v for k, v in try_proxy.items() if not k.startswith("_")}

                try:
                    async with AsyncCamoufox(**camoufox_kwargs) as browser:
                        page = await browser.new_page()
                        result = await _run_page(page)
                        total_s = time.time() - _attempt_start
                        # _cf_elapsed[0] closure üzerinden dolduruldu (Turnstile geçiş süresi)
                        _cf_s = _cf_elapsed[0] if _cf_elapsed[0] > 0 else result.get("_cf_secs", 0.0)
                        if not _is_proxy_network_error(result) and not _is_rate_limited(result) and not _is_account_restricted(result) and not _is_session_expired(result):
                            log_attempt(proxy_server, acct_email, "success", cf_secs=_cf_s, total_secs=total_s)
                            _account_last_used[acct_email] = time.time()
                            return result
                        last_result = result
                        if _is_session_expired(result):
                            log_attempt(proxy_server, acct_email, "error", total_secs=total_s)
                            logger.warning(f"Session sona erdi — fresh login ile tekrar deneniyor: {acct_email}")
                            continue  # Aynı proxy, cookie silindi — fresh login yapacak
                        if _is_account_restricted(result):
                            log_attempt(proxy_server, acct_email, "account_restricted", cf_secs=_cf_s, total_secs=total_s)
                            # 24 saat cooldown — manuel çözüm gerekiyor
                            set_account_cooldown(acct_email, 86400, "account_restricted")
                            logger.error(f"⛔ Hesap kısıtlandı (24s cooldown) — VFS support gerekli: {acct_email}")
                            break  # Proxy değiştirmenin faydası yok, sonraki hesaba geç
                        if _is_rate_limited(result):
                            log_attempt(proxy_server, acct_email, "rate_limited", cf_secs=_cf_s, total_secs=total_s)
                            _cd = _adaptive_cooldown_secs(acct_email)
                            set_account_cooldown(acct_email, _cd, "rate_limit")
                            logger.warning(f"Rate limit — {_cd}s cooldown eklendi, sonraki hesaba geçiliyor: {acct_email}")
                            await _sleep(15)
                            break  # Bu hesap rate limited, proxy döngüsünü kır, sonraki hesaba geç
                        log_attempt(proxy_server, acct_email, "error", total_secs=total_s)
                        logger.info(f"Proxy ağ hatası, yeni proxy deneniyor: {result.get('matched_text','')[:60]}")
                except Exception as e:
                    err_msg = str(e)
                    if err_msg in ("cf_ip_rotated", "cf_stuck"):
                        logger.warning(f"CF geçilemedi ({err_msg}) — yeni browser session ile tekrar deneniyor")
                        log_attempt(proxy_server, acct_email, "error")
                        last_result = {"ok": False, "status_label": "ERROR", "matched_text": err_msg, "signature": err_msg}
                        continue  # Aynı proxy, yeni IP ile tekrar dene
                    if "connect to proxy" in err_msg.lower() or "proxy" in err_msg.lower():
                        logger.warning(f"Proxy bağlanamadı (geoip=False ile yeniden dene): {err_msg[:80]}")
                        try:
                            kw2 = dict(camoufox_kwargs)
                            kw2["geoip"] = False
                            async with AsyncCamoufox(**kw2) as browser:
                                page = await browser.new_page()
                                result = await _run_page(page)
                                total_s2 = time.time() - _attempt_start
                                if not _is_proxy_network_error(result) and not _is_rate_limited(result) and not _is_account_restricted(result) and not _is_session_expired(result):
                                    log_attempt(proxy_server, acct_email, "success", total_secs=total_s2)
                                    return result
                                last_result = result
                                if _is_account_restricted(result):
                                    log_attempt(proxy_server, acct_email, "account_restricted", total_secs=total_s2)
                                    set_account_cooldown(acct_email, 86400, "account_restricted")
                                    logger.error(f"⛔ Hesap kısıtlandı (geoip=False) — VFS support gerekli: {acct_email}")
                                    break
                                if _is_rate_limited(result):
                                    log_attempt(proxy_server, acct_email, "rate_limited", total_secs=total_s2)
                                    _cd2 = _adaptive_cooldown_secs(acct_email)
                                    set_account_cooldown(acct_email, _cd2, "rate_limit")
                                    logger.warning(f"Rate limit (geoip=False) — {_cd2}s cooldown: {acct_email}")
                                    await _sleep(15)
                                    break
                                log_attempt(proxy_server, acct_email, "error", total_secs=total_s2)
                        except Exception as e2:
                            logger.error(f"Proxy geoip=False ile de başarısız: {e2}")
                            log_attempt(proxy_server, acct_email, "error")
                            last_result = {"ok": False, "status_label": "ERROR", "matched_text": f"Proxy bağlantı hatası: {str(e2)[:60]}", "signature": "proxy_error"}
                    else:
                        logger.error(f"Browser başlatma hatası: {e}")
                        log_attempt(proxy_server, acct_email, "error")
                        last_result = {"ok": False, "status_label": "ERROR", "matched_text": f"Browser hatası: {str(e)[:60]}", "signature": "error"}

                # Hesaba özel proxy hata verdiyse ve IP reset URL tanımlıysa:
                # henüz reset yapılmadıysa yap, bir sonraki proxy iterasyonu (aynı proxy) ile retry
                if _ip_reset_url and not _ip_reset_done and _is_proxy_network_error(last_result):
                    _ip_reset_done = True
                    logger.info(f"IP reset URL çağrılıyor (proxy hata sonrası): {_ip_reset_url}")
                    try:
                        await asyncio.to_thread(urllib.request.urlopen, _ip_reset_url, timeout=15)
                        logger.info("IP reset başarılı — yeni IP bekleniyor...")
                    except Exception as _re:
                        logger.warning(f"IP reset isteği başarısız (devam ediliyor): {_re}")
                    _reset_wait = random.uniform(10.0, 15.0)
                    logger.info(f"IP reset sonrası {_reset_wait:.0f}s bekleniyor...")
                    await asyncio.sleep(_reset_wait)

            # Hesap değişimi: sonraki farklı hesap varsa IP rotasyonu yap
            # (aynı IP ile farklı mail kullanılmasın — block riskini azaltır)
            _has_next_acct = any(
                get_cooldown_remaining(account_pool[i]["email"]) <= 0
                for i in range(acct_idx + 1, len(account_pool))
            )
            if _has_next_acct:
                _global_reset_url = ""
                try:
                    _rot_file = DATA_DIR / "proxy_rotation.json"
                    if _rot_file.exists():
                        _rot_data = json.loads(_rot_file.read_text(encoding="utf-8"))
                        if _rot_data:
                            _global_reset_url = list(_rot_data.values())[0].get("url", "")
                except Exception:
                    pass
                if _global_reset_url:
                    logger.info(f"Hesap değişimi ({acct_email} → sonraki hesap) — IP rotasyonu yapılıyor")
                    try:
                        await asyncio.to_thread(urllib.request.urlopen, _global_reset_url, timeout=15)
                        logger.info("IP rotasyonu başarılı — sonraki hesap yeni IP ile denenecek")
                        await asyncio.sleep(random.uniform(10.0, 15.0))
                    except Exception as _re:
                        logger.warning(f"IP rotasyon hatası (devam ediliyor): {_re}")

        return last_result

    # Diğer sağlayıcılar: Playwright Chromium + stealth
    _rand_ua = random.choice(_UA_POOL)
    _rand_vw, _rand_vh = random.choice(_VIEWPORT_POOL)
    # UA ile platform tutarlı olsun: macOS UA → MacIntel, Windows UA → Win32
    _plat_override = "MacIntel" if "Macintosh" in _rand_ua or "Mac OS X" in _rand_ua else "Win32"
    _stealth = Stealth(
        navigator_languages_override=random.choice([
            ("tr-TR", "tr", "en-US", "en"),
            ("tr-TR", "tr", "en-GB", "en"),
            ("tr-TR", "en-US", "tr"),
        ]),
        navigator_platform_override=_plat_override,
        navigator_user_agent_override=_rand_ua,
    )
    launch_args = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            f"--window-size={_rand_vw},{_rand_vh}",
        ],
    }
    if proxy:
        launch_args["proxy"] = proxy

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context(
            viewport={"width": _rand_vw, "height": _rand_vh},
            user_agent=_rand_ua,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            color_scheme="light",
        )
        await _stealth.apply_stealth_async(context)
        page = await context.new_page()
        try:
            return await _run_page(page)
        finally:
            await context.close()
            await browser.close()


class VisaWatchService:
    def __init__(self) -> None:
        self.sites = [s for s in load_sites() if s.get("enabled", True)]
        self.state = load_state()
        self.proxies = load_proxies()
        self.app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        _bot_ref[0] = self.app.bot  # admin photo gönderimi için global referans
        self._booking_queues: dict[int, asyncio.Queue] = {}
        # Session-based auto-monitor: chat_id → {site_key, email, password, center_text, ...}
        self._autobook_pending: dict[int, dict] = {}
        self.auto_book_manager = AutoBookManager(
            sites=self.sites,
            proxies=self.proxies,
            notify_admin=self._notify_admin_text,
        )
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("menu", self.cmd_start))
        self.app.add_handler(CommandHandler("check", self.cmd_start))
        self.app.add_handler(CommandHandler("musteriler", self.cmd_customers))
        self.app.add_handler(CommandHandler("istatistik", self.cmd_stats))
        self.app.add_handler(CommandHandler("takip", self.cmd_takip))
        self.app.add_handler(CommandHandler("restart", self.cmd_restart))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        # Private + grup reply + booking queue (grup privacy modu kapalıysa her metin gelir)
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.REPLY | filters.ChatType.GROUPS),
            self.handle_text
        ))

        # Müşteri ekleme adım akışı için chat_id → dict haritası
        self._customer_wizards: dict[int, dict] = {}   # {chat_id: {"step": int, "data": Customer}}

    def _is_allowed(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id in ALLOWED_USERS

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        if not self._is_allowed(update):
            return
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        logger.info(f"handle_text: chat_id={chat_id} wizard={'evet' if chat_id in self._customer_wizards else 'yok'} booking={'evet' if chat_id in self._booking_queues else 'yok'} text={text[:30]!r}")

        # Aktif booking akışı varsa ona ilet
        if chat_id in self._booking_queues:
            await self._booking_queues[chat_id].put(text)
            return

        # Müşteri ekleme wizard'ı aktifse devam et
        if chat_id in self._customer_wizards:
            await self._customer_wizard_step(update, text)
            return

        # Arama modu: "/" ile başlamayan metin → müşteri ara
        if text and not text.startswith("/"):
            results = search_customers(text)
            if results:
                rows = [
                    [InlineKeyboardButton(c.to_text(short=True), callback_data=f"cust_detail:{c.id}")]
                    for c in results[:10]
                ]
                await update.message.reply_text(
                    f"🔍 '{text}' için {len(results)} sonuç:",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            else:
                await update.message.reply_text(
                    f"❌ '{text}' için müşteri bulunamadı.\n/musteriler — tüm liste"
                )

    def build_menu(self) -> InlineKeyboardMarkup:
        unique = []
        seen = set()

        for s in self.sites:
            g = s.get("group_key", s["key"])
            if g in seen:
                continue
            seen.add(g)
            # Bayrakları kaldır: sadece metin bırak
            label = s.get("menu_label") or s.get("name", "")
            label = re.sub(r"[\U0001F1E0-\U0001F1FF]", "", label).strip()
            unique.append((g, label))

        rows = []
        row = []
        for g, label in unique:
            row.append(InlineKeyboardButton(label, callback_data=f"country:{g}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        rows.append([InlineKeyboardButton("Menu ⚙️", callback_data="menu")])
        return InlineKeyboardMarkup(rows)

    def _customer_panel_markup(self) -> InlineKeyboardMarkup:
        abm = self.auto_book_manager
        active_sess = sum(1 for t in abm._session_tasks.values() if not t.done())
        active_cust = abm.active_count()
        ab_badge = f" 🔴{active_sess + active_cust}" if (active_sess + active_cust) else ""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Randevu Al", callback_data="randevu_al")],
            [InlineKeyboardButton("➕ Yeni Müşteri Ekle", callback_data="cust_add")],
            [InlineKeyboardButton("📋 Tüm Müşteriler", callback_data="cust_list:all:0"),
             InlineKeyboardButton("⏳ Bekleyenler", callback_data="cust_list:bekliyor:0")],
            [InlineKeyboardButton("✅ Randevu Alınanlar", callback_data="cust_list:randevu_alindi:0")],
            [InlineKeyboardButton(f"🤖 Otomatik Takipler{ab_badge}", callback_data="autobook_panel")],
            [InlineKeyboardButton("📊 İstatistik", callback_data="istatistik")],
        ])

    def _customer_panel_text(self) -> str:
        counts = customer_count()
        total = sum(counts.values())
        return (
            f"👥 *Müşteri Paneli*\n\n"
            f"📋 Toplam: {total} müşteri\n"
            f"⏳ Bekliyor: {counts.get('bekliyor', 0)}\n"
            f"✅ Randevu alındı: {counts.get('randevu_alindi', 0)}\n"
            f"🎉 Tamamlandı: {counts.get('tamamlandi', 0)}\n\n"
            f"Arama için isim veya pasaport no yazın."
        )

    def _autobook_panel_text(self) -> str:
        abm = self.auto_book_manager
        sess_count = sum(1 for t in abm._session_tasks.values() if not t.done())
        cust_count  = abm.active_count()
        lines = ["🤖 *Otomatik Takip Paneli*\n"]
        lines.append(f"👁 Session takipler: *{sess_count}* aktif")
        lines.append(f"🤖 Müşteri otomasyonları: *{cust_count}* aktif")
        if sess_count == 0 and cust_count == 0:
            lines.append("\n_Aktif takip yok._")
            lines.append("Randevu kontrolü yaptıktan sonra CLOSED sonucundan")
            lines.append("veya müşteri detayından takip başlatabilirsiniz.")
        return "\n".join(lines)

    def _autobook_panel_markup(self, chat_id: int) -> InlineKeyboardMarkup:
        abm = self.auto_book_manager
        rows = []

        # Session takipler
        for sid, task in list(abm._session_tasks.items()):
            if task.done():
                continue
            label = f"👁 Session [{sid}]" if sid != chat_id else "👁 Aktif Session Takibim"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"autobook_session_info:{sid}"),
                InlineKeyboardButton("⏹️ Durdur", callback_data=f"autobook_session_stop:{sid}"),
            ])

        # Müşteri otomasyonları
        for cid, task in list(abm._tasks.items()):
            if task.done():
                continue
            from customer_db import get_customer as _gc
            c = _gc(cid)
            name = c.name if c else f"#{cid}"
            mode_icon = "🤖" if (c and c.auto_book_mode == "auto") else "👁"
            site_key  = c.auto_book_site_key if c else "?"
            rows.append([
                InlineKeyboardButton(f"{mode_icon} {name} [{site_key}]",
                                     callback_data=f"cust_detail:{cid}"),
                InlineKeyboardButton("⏹️ Durdur", callback_data=f"autobook_stop_cust:{cid}"),
            ])

        rows.append([InlineKeyboardButton("🔄 Yenile", callback_data="autobook_panel"),
                     InlineKeyboardButton("🏠 Ana Menü", callback_data="ana_menu")])
        return InlineKeyboardMarkup(rows)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            self._customer_panel_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._customer_panel_markup(),
        )

    # ─────────────────────────── MÜŞTERİ YÖNETİMİ ────────────────────────────

    async def cmd_customers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            self._customer_panel_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._customer_panel_markup(),
        )

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        summary = stats_summary(window_hours=24)
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

    async def cmd_takip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        chat_id = update.effective_chat.id
        await update.message.reply_text(
            self._autobook_panel_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=self._autobook_panel_markup(chat_id),
        )

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        await update.message.reply_text(
            "🔄 *Bot yeniden başlatılıyor...*\n\n"
            "✅ Birkaç saniye içinde tekrar çevrimiçi olacak.",
            parse_mode=ParseMode.MARKDOWN,
        )
        logger.info("Kullanıcı isteğiyle bot yeniden başlatılıyor...")
        await asyncio.sleep(1)
        import os, sys
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ─────────────────── MÜŞTERİ EKLEME WIZARD ───────────────────────────────
    # step_type: "text" → ForceReply,  "button" → InlineKeyboard (dinamik)
    # Sıra: destination ÖNCE visa_type'tan gelir, böylece visa_type butonu context'e göre dolar

    WIZARD_STEPS = [
        # (field,             prompt,                                                    step_type)
        ("name",             "👤 *Ad Soyad* girin:",                                    "text"),
        ("passport_no",      "🛂 *Pasaport No* girin:",                                 "text"),
        ("nationality",      "🌍 *Uyruk* seçin:",                                       "button"),
        ("destination",      "✈️ *Hedef Ülke* seçin:",                                  "button"),
        ("visa_type",        "📋 *Vize Tipi* seçin:",                                   "button"),
        ("travel_date",      "🗓 *Seyahat Tarihi* girin \\(gg\\.aa\\.yyyy veya \\-\\):","text"),
        ("phone",            "📞 *Telefon* girin \\(\\- = boş\\):",                     "text"),
        ("vfs_email",    "🔑 *VFS E-posta* girin \\(\\- = boş\\):",  "text"),
        ("vfs_password", "🔒 *VFS Şifre* girin \\(\\- = boş\\):",   "text"),
        ("notes",        "📝 *Notlar* girin \\(\\- = boş\\):",       "text"),
    ]

    # Uyruk statik fallback (cache dolmadan önce kullanılır)
    _NATIONALITY_DEFAULTS = [
        "Turkey", "Russian Federation", "Ukraine",
        "Syrian Arab Republic", "Azerbaijan", "Georgia",
        "Uzbekistan", "Kazakhstan",
    ]

    def _get_nationality_options(self) -> list[str]:
        cache = load_vfs_options_cache()
        cached = cache.get("nationalities", [])
        return cached if cached else self._NATIONALITY_DEFAULTS

    def _get_destination_options(self) -> list[str]:
        seen: list[str] = []
        seen_keys: set[str] = set()
        for s in self.sites:
            g = s.get("group_key", s["key"])
            if g in seen_keys:
                continue
            seen_keys.add(g)
            name = re.sub(r"[\U0001F1E0-\U0001F1FF]", "", s.get("menu_label") or s.get("name", "")).strip()
            seen.append(name)
        return seen

    def _get_visa_type_options(self, destination: str) -> list[str]:
        dest_l = destination.lower()
        for s in self.sites:
            name_l = s.get("name", "").lower()
            if name_l == dest_l or dest_l in name_l or name_l in dest_l:
                subcats = s.get("vfs_options", {}).get("subcategories", [])
                if subcats:
                    return subcats
        return ["Tourism / Turistik", "Business / Ticari", "Family Visit / Aile Ziyareti",
                "Student / Ogrenci", "Transit / Transit", "Other / Diger"]

    def _get_vfs_account_options(self) -> list[str]:
        """sites.json'daki tüm VFS hesap e-postalarını listeler."""
        emails: list[str] = []
        seen: set[str] = set()
        for s in self.sites:
            if s.get("provider", "").upper() != "VFS":
                continue
            for acct in s.get("accounts", []):
                em = acct.get("email", "").strip()
                if em and em not in seen:
                    seen.add(em)
                    emails.append(em)
            # Accounts listesi yoksa site düzeyindeki e-postayı ekle
            if not s.get("accounts") and s.get("email"):
                em = s["email"].strip()
                if em and em not in seen:
                    seen.add(em)
                    emails.append(em)
        return emails

    def _build_wizard_buttons(self, field: str, customer: "Customer") -> tuple["InlineKeyboardMarkup", list[str]]:
        if field == "nationality":
            options = self._get_nationality_options()
        elif field == "destination":
            options = self._get_destination_options()
        elif field == "visa_type":
            options = self._get_visa_type_options(customer.destination)
        else:
            options = []

        step_idx = next(i for i, (f, _, _) in enumerate(self.WIZARD_STEPS) if f == field)
        rows = []
        row = []
        for i, opt in enumerate(options):
            label = opt[:28]  # buton etiket max uzunluğu
            row.append(InlineKeyboardButton(label, callback_data=f"wiz:{step_idx}:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("✏️ Diğer (yaz)", callback_data=f"wiz:{step_idx}:other")])
        return InlineKeyboardMarkup(rows), options

    async def _send_wizard_step(self, chat_id: int, step: int, reply_func) -> None:
        field, prompt, step_type = self.WIZARD_STEPS[step]
        wizard = self._customer_wizards[chat_id]
        header = "📝 *Yeni Müşteri Ekleme*\n\n" if step == 0 else ""
        if step_type == "text":
            await reply_func(
                f"{header}{prompt}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=ForceReply(selective=True),
            )
        else:
            markup, choices = self._build_wizard_buttons(field, wizard["data"])
            wizard["choices"][step] = choices
            await reply_func(
                f"{header}{prompt}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )

    async def _start_customer_wizard(self, chat_id: int, reply_func) -> None:
        self._customer_wizards[chat_id] = {"step": 0, "data": Customer(), "choices": {}}
        logger.info(f"Wizard başladı: chat_id={chat_id}")
        await self._send_wizard_step(chat_id, 0, reply_func)

    async def _wizard_finish(self, chat_id: int, reply_func) -> None:
        wizard = self._customer_wizards.pop(chat_id)
        customer: Customer = wizard["data"]
        new_id = add_customer(customer)
        customer.id = new_id
        logger.info(f"Müşteri kaydedildi: id={new_id} isim={customer.name!r}")
        await reply_func(
            f"✅ *Müşteri kaydedildi!*\n\n{customer.to_text()}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Detay Gör", callback_data=f"cust_detail:{new_id}")],
            ]),
        )

    async def _customer_wizard_step(self, update: Update, text: str) -> None:
        """Yazı giriş adımlarını işler (hem normal hem 'Diğer' buton sonrası)."""
        chat_id = update.effective_chat.id
        wizard = self._customer_wizards.get(chat_id)
        if not wizard:
            logger.warning(f"Wizard bulunamadı: chat_id={chat_id}")
            return

        try:
            value = "" if text.strip() in ("-", "") else text.strip()

            # "Diğer (yaz)" buton sonrası belirli bir adım bekleniyor
            if "awaiting_text_step" in wizard:
                step_idx = wizard.pop("awaiting_text_step")
                field, _, _ = self.WIZARD_STEPS[step_idx]
                setattr(wizard["data"], field, value)
                logger.info(f"Wizard text (Diğer) adım {step_idx}: {field}={value!r}")
                next_step = step_idx + 1
            else:
                step = wizard["step"]
                field, _, step_type = self.WIZARD_STEPS[step]
                # Buton adımında yazı gelirse → butonları yeniden göster (kullanıcıya bildir)
                if step_type == "button":
                    await update.message.reply_text(
                        "⬆️ Lütfen yukarıdaki butonlardan seçin ya da *Diğer (yaz)* butonuna basın.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return
                setattr(wizard["data"], field, value)
                logger.info(f"Wizard text adım {step}: {field}={value!r}")
                next_step = step + 1

            wizard["step"] = next_step

            if next_step < len(self.WIZARD_STEPS):
                await self._send_wizard_step(chat_id, next_step, update.message.reply_text)
            else:
                await self._wizard_finish(chat_id, update.message.reply_text)

        except Exception as e:
            logger.exception(f"Wizard adım hatası: {e}")
            await update.message.reply_text(f"⚠️ Hata: {e}\nTekrar başlatın: /musteriler")
            self._customer_wizards.pop(chat_id, None)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.callback_query.answer()
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        # Navigasyon: mevcut mesajı düzenle (aynı blok), booking/wizard için reply
        async def edit(text: str, markup=None, md: bool = True):
            try:
                await query.message.edit_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN if md else None,
                    reply_markup=markup,
                )
            except Exception:
                await query.message.reply_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN if md else None,
                    reply_markup=markup,
                )

        back_menu = [[InlineKeyboardButton("🏠 Ana Menü", callback_data="ana_menu")]]

        # ─────────── WIZARD BUTON CALLBACK'LERİ ───────────

        if data.startswith("wiz:"):
            parts = data.split(":", 2)
            step_idx = int(parts[1])
            choice = parts[2]
            chat_id = query.message.chat_id
            wizard = self._customer_wizards.get(chat_id)
            if not wizard:
                await query.message.reply_text("⚠️ Wizard oturumu sona erdi. Tekrar başlatın: /musteriler")
                return
            if choice == "other":
                # Kullanıcı "Diğer (yaz)" seçti — bu adım için yazı bekliyoruz
                field, prompt, _ = self.WIZARD_STEPS[step_idx]
                wizard["awaiting_text_step"] = step_idx
                await query.message.reply_text(
                    f"{prompt}\n_\\(veya \\- yazın boş bırakmak için\\)_",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=ForceReply(selective=True),
                )
                return
            # Buton seçimi
            choices = wizard["choices"].get(step_idx, [])
            try:
                value = choices[int(choice)]
            except (IndexError, ValueError):
                value = choice
            field, _, _ = self.WIZARD_STEPS[step_idx]
            setattr(wizard["data"], field, value)
            logger.info(f"Wizard button adım {step_idx}: {field}={value!r}")
            next_step = step_idx + 1
            wizard["step"] = next_step
            # Seçimi onayla (kısa mesaj)
            await query.message.reply_text(f"✅ {value}")
            if next_step < len(self.WIZARD_STEPS):
                await self._send_wizard_step(chat_id, next_step, query.message.reply_text)
            else:
                await self._wizard_finish(chat_id, query.message.reply_text)
            return

        # ─────────── MÜŞTERİ CALLBACK'LERİ ───────────

        if data == "cust_add":
            chat_id = query.message.chat_id
            await self._start_customer_wizard(chat_id, query.message.reply_text)
            return

        if data.startswith("cust_list:"):
            _, status_filter, offset_str = data.split(":")
            offset = int(offset_str)
            customers = list_customers(
                status=None if status_filter == "all" else status_filter,
                limit=10, offset=offset,
            )
            if not customers:
                await edit("📭 Kayıt bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            rows = [
                [InlineKeyboardButton(c.to_text(short=True), callback_data=f"cust_detail:{c.id}")]
                for c in customers
            ]
            nav = []
            if offset > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"cust_list:{status_filter}:{offset-10}"))
            if len(customers) == 10:
                nav.append(InlineKeyboardButton("▶", callback_data=f"cust_list:{status_filter}:{offset+10}"))
            if nav:
                rows.append(nav)
            rows.append([InlineKeyboardButton("➕ Yeni Müşteri", callback_data="cust_add"),
                         InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            label = "Tüm Müşteriler" if status_filter == "all" else status_filter.replace("_", " ").title()
            await edit(f"👥 *{label}* ({offset+1}–{offset+len(customers)})", InlineKeyboardMarkup(rows))
            return

        if data.startswith("cust_detail:"):
            cid = int(data.split(":")[1])
            c = get_customer(cid)
            if not c:
                await edit("❌ Müşteri bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            ab_status = self.auto_book_manager.status(cid)
            ab_active = c.auto_book_enabled and cid in self.auto_book_manager._tasks and not self.auto_book_manager._tasks[cid].done()
            _ab_mode_label = "Randevu" if c.auto_book_mode == "auto" else "Takip"
            ab_btn_label = (f"⏸️ Otomatik {_ab_mode_label} Durdur [{ab_status}]"
                            if ab_active else f"🤖 Otomatik Randevu Al [{ab_status}]")
            buttons = [
                [InlineKeyboardButton("📅 Randevu Al", callback_data=f"cust_book_country:{cid}")],
                [InlineKeyboardButton(ab_btn_label, callback_data=f"cust_autobook:{cid}")],
                [InlineKeyboardButton("✏️ Durum Güncelle", callback_data=f"cust_status_menu:{cid}"),
                 InlineKeyboardButton("🗑️ Sil", callback_data=f"cust_delete_confirm:{cid}")],
                [InlineKeyboardButton("◀ Liste", callback_data="cust_list:all:0"),
                 InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
            ]
            await edit(c.to_text(), InlineKeyboardMarkup(buttons))
            return

        if data.startswith("cust_book_country:"):
            cid = int(data.split(":")[1])
            c = get_customer(cid)
            if not c:
                await edit("❌ Müşteri bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            unique = []
            seen = set()
            for s in self.sites:
                g = s.get("group_key", s["key"])
                if g in seen:
                    continue
                seen.add(g)
                menu_label = s.get("menu_label") or s.get("name", "")
                flags = re.findall(r"[\U0001F1E0-\U0001F1FF]{2}", menu_label)
                name_only = re.sub(r"[\U0001F1E0-\U0001F1FF]", "", menu_label).strip()
                unique.append((g, f"{flags[0]} {name_only}" if flags else name_only))
            rows = [
                [InlineKeyboardButton(label, callback_data=f"cust_book_site:{cid}:{g}") for g, label in unique[i:i+2]]
                for i in range(0, len(unique), 2)
            ]
            rows.append([InlineKeyboardButton("◀ Geri", callback_data=f"cust_detail:{cid}"),
                         InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit(f"📅 *{c.name}* için ülke seçin:", InlineKeyboardMarkup(rows))
            return

        if data.startswith("cust_book_site:"):
            _, cid_str, group_key = data.split(":")
            cid = int(cid_str)
            c = get_customer(cid)
            matched_sites = [s for s in self.sites if s.get("group_key", s["key"]) == group_key]
            if not matched_sites or not c:
                await edit("❌ Bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            first = matched_sites[0]
            if first.get("provider", "").upper() == "VFS":
                buttons = [
                    [InlineKeyboardButton("📅 Randevu Al (Canlı Merkez Seçimi)", callback_data=f"cust_vfs_center:{cid}:{first['key']}:live")],
                    [InlineKeyboardButton("◀ Geri", callback_data=f"cust_book_country:{cid}"),
                     InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
                ]
                await edit(
                    f"🏢 *{c.name}* — *{first['name']}* randevu işlemi\n\n"
                    f"Giriş sonrası VFS'ten canlı merkez listesi gösterilecek.",
                    InlineKeyboardMarkup(buttons)
                )
            else:
                await query.message.reply_text(f"⏳ {c.name} için {first['name']} kontrol ediliyor...")
                result = await fetch_site(first, self.proxies, notify_func=query.message.reply_text)
                update_customer_status(cid, "randevu_alindi" if result["status_label"] == "BOOKED" else c.status)
                await query.message.reply_text(format_message(first, result, 0), parse_mode=ParseMode.MARKDOWN)
            return

        if data.startswith("cust_vfs_center:"):
            parts = data.split(":")
            cid, site_key, center_idx = int(parts[1]), parts[2], parts[3]
            c = get_customer(cid)
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site or not c:
                return

            # "live" → giriş sonrası VFS'ten canlı merkez seçimi
            if center_idx == "live":
                center_text = ""
            else:
                center_text = site["vfs_options"]["centers"][int(center_idx)]
            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await query.message.reply_text("⚠️ Zaten aktif bir başvuru akışı var.")
                return

            # Kategori ve alt kategori login sonrası dinamik çekilecek
            dynamic_config_cvc = {
                "center_text":     center_text,
                "subcategory_text": c.visa_type if c.visa_type else "",
            }
            q_cvc: asyncio.Queue = asyncio.Queue()
            self._booking_queues[chat_id] = q_cvc

            name_parts = c.name.strip().split()
            _auto_cvc = {
                "first name":  name_parts[0] if name_parts else "",
                "ad (first":   name_parts[0] if name_parts else "",
                "last name":   " ".join(name_parts[1:]) if len(name_parts) > 1 else (name_parts[0] if name_parts else ""),
                "soyad (last": " ".join(name_parts[1:]) if len(name_parts) > 1 else (name_parts[0] if name_parts else ""),
                "pasaport num": c.passport_no,
                "passport num": c.passport_no,
                "vatandaşlık":  c.nationality,
                "nationality":  c.nationality,
            }

            async def wait_input_cvc(prompt: str, buttons=None):
                prompt_lower = prompt.lower()
                for key, val in _auto_cvc.items():
                    if key in prompt_lower and val:
                        await query.message.reply_text(
                            f"✅ *{prompt.splitlines()[0]}*\n`{val}` _(müşteri kaydından)_",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        return val
                kwargs = {"parse_mode": ParseMode.MARKDOWN}
                if buttons:
                    kwargs["reply_markup"] = InlineKeyboardMarkup(buttons)
                sent_msg = await query.message.reply_text(prompt, **kwargs)
                try:
                    value = await asyncio.wait_for(q_cvc.get(), timeout=300)
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    return value
                except asyncio.TimeoutError:
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    await query.message.reply_text("⏱ Zaman aşımı — iptal edildi.")
                    return None

            async def notify_cvc(msg: str):
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            async def _run_cvc_task():
                try:
                    action_buttons = [
                        [InlineKeyboardButton("📅 Yeni Randevu Al",           callback_data="vfs_pick:__yeni__")],
                        [InlineKeyboardButton("📂 Mevcut Başvurularımı Gör", callback_data="vfs_pick:__sorgula__")],
                    ]
                    _center_line_cvc = f"📍 *{center_text}*" if center_text else "🏢 Merkez giriş sonrası VFS'ten seçilecek"
                    action = await wait_input_cvc(
                        f"👤 *{c.name}* — {_center_line_cvc}\n\n🎯 Yapmak istediğiniz işlemi seçin:",
                        action_buttons,
                    )
                    if not action:
                        return
                    dynamic_config_cvc["_action"] = action

                    if c.vfs_email and c.vfs_password:
                        await query.message.reply_text(
                            f"✅ VFS giriş bilgileri müşteri kaydından alındı: `{c.vfs_email}`",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        dynamic_config_cvc["email"] = c.vfs_email
                        dynamic_config_cvc["password"] = c.vfs_password
                        dynamic_config_cvc["imap_password"] = ""
                    else:
                        email = await wait_input_cvc("📧 *VFS hesap e-posta adresinizi girin:*")
                        if not email:
                            return
                        password = await wait_input_cvc("🔑 *VFS hesap şifrenizi girin:*")
                        if not password:
                            return
                        dynamic_config_cvc["email"] = email.strip()
                        dynamic_config_cvc["password"] = password.strip()
                    await query.message.reply_text(
                        f"🔄 *{c.name}* için VFS'e bağlanılıyor...\n"
                        f"📋 Ad/soyad ve pasaport bilgileri müşteri kaydından otomatik doldurulacak.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    result = await fetch_site(
                        site, self.proxies, dynamic_config_cvc,
                        notify_func=notify_cvc,
                        booking_mode=True,
                        wait_input_func=wait_input_cvc,
                    )
                    if result["status_label"] == "BOOKED":
                        update_customer_status(cid, "randevu_alindi",
                                               appt_ref=result.get("matched_text", "")[:50],
                                               center=center_text)
                    await notify_cvc(format_message(site, result, 0, dynamic_config_cvc))
                    # CLOSED sonucunda otomatik takip teklifi — session verisini sakla
                    if result["status_label"] == "CLOSED":
                        self._autobook_pending[chat_id] = {
                            "site_key":        site["key"],
                            "email":           dynamic_config_cvc.get("email", ""),
                            "password":        dynamic_config_cvc.get("password", ""),
                            "center_text":     dynamic_config_cvc.get("center_text", ""),
                            "category_text":   dynamic_config_cvc.get("category_text", ""),
                            "subcategory_text":dynamic_config_cvc.get("subcategory_text", ""),
                        }
                        await query.message.reply_text(
                            "💡 Slot bulunamadı. Aynı merkez/kategori kombinasyonu için "
                            "otomatik takip başlatmak ister misiniz?\n"
                            "_Slot açıldığında bildirim gelir — rezervasyon manuel yapılır._",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("👁 Otomatik Takip Başlat",
                                                     callback_data="autobook_from_session"),
                            ]]),
                        )
                except Exception as e:
                    logger.exception("cust_vfs_center booking hatası")
                    await notify_cvc(f"❌ Hata: {str(e)[:200]}")
                finally:
                    self._booking_queues.pop(chat_id, None)

            asyncio.create_task(_run_cvc_task())
            return

        if data.startswith("cust_vfs_cat:"):
            parts = data.split(":")
            cid, site_key, center_idx, cat_idx = int(parts[1]), parts[2], parts[3], parts[4]
            c = get_customer(cid)
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site or not c:
                return
            cat_text = site["vfs_options"]["categories"][int(cat_idx)]
            cat_sub_map = site["vfs_options"].get("category_subcategory_map", {})
            subcategories = cat_sub_map.get(cat_text) or site["vfs_options"].get("subcategories", [])
            buttons = [
                [InlineKeyboardButton(sub[:48], callback_data=f"cust_vfs_sub:{cid}:{site_key}:{center_idx}:{cat_idx}:{i}")]
                for i, sub in enumerate(subcategories)
            ]
            buttons.append([InlineKeyboardButton("◀ Geri", callback_data=f"cust_vfs_center:{cid}:{site_key}:{center_idx}"),
                             InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit(f"📄 *{c.name}* — Alt kategori seçin:", InlineKeyboardMarkup(buttons))
            return

        if data.startswith("cust_vfs_sub:"):
            parts = data.split(":")
            cid, site_key, center_idx, cat_idx, sub_idx = int(parts[1]), parts[2], parts[3], parts[4], parts[5]
            c = get_customer(cid)
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site or not c:
                return
            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await query.message.reply_text("⚠️ Zaten aktif bir başvuru akışı var.")
                return
            cat_text = site["vfs_options"]["categories"][int(cat_idx)]
            cat_sub_map = site["vfs_options"].get("category_subcategory_map", {})
            subcategories = cat_sub_map.get(cat_text) or site["vfs_options"].get("subcategories", [])
            dynamic_config = {
                "center_text": site["vfs_options"]["centers"][int(center_idx)],
                "category_text": cat_text,
                "subcategory_text": subcategories[int(sub_idx)],
            }
            q: asyncio.Queue = asyncio.Queue()
            self._booking_queues[chat_id] = q

            # Müşteri verisi → form alanları otomatik doldurma haritası
            name_parts = c.name.strip().split()
            _auto = {
                "first name": name_parts[0] if name_parts else "",
                "ad (first": name_parts[0] if name_parts else "",
                "last name":  " ".join(name_parts[1:]) if len(name_parts) > 1 else name_parts[0] if name_parts else "",
                "soyad (last": " ".join(name_parts[1:]) if len(name_parts) > 1 else name_parts[0] if name_parts else "",
                "pasaport num": c.passport_no,
                "passport num": c.passport_no,
                "vatandaşlık":  c.nationality,
                "nationality":  c.nationality,
            }

            async def wait_input_cust(prompt: str, buttons=None):
                # Otomatik doldurulabilecek alan mı?
                prompt_lower = prompt.lower()
                for key, val in _auto.items():
                    if key in prompt_lower and val:
                        await query.message.reply_text(
                            f"✅ *{prompt.splitlines()[0]}*\n`{val}` _(müşteri kaydından)_",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        return val
                # Manuel giriş gerekiyor
                kwargs = {"parse_mode": ParseMode.MARKDOWN}
                if buttons:
                    kwargs["reply_markup"] = InlineKeyboardMarkup(buttons)
                sent_msg = await query.message.reply_text(prompt, **kwargs)
                try:
                    value = await asyncio.wait_for(q.get(), timeout=300)
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    return value
                except asyncio.TimeoutError:
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    await query.message.reply_text("⏱ Zaman aşımı — iptal edildi.")
                    return None

            async def notify_cust(msg: str):
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            async def _run_cust_booking():
                try:
                    # 1. Login öncesi işlem seçimi
                    action_buttons = [
                        [InlineKeyboardButton("📅 Yeni Randevu Al",           callback_data="vfs_pick:__yeni__")],
                        [InlineKeyboardButton("📂 Mevcut Başvurularımı Gör", callback_data="vfs_pick:__sorgula__")],
                    ]
                    action = await wait_input_cust(
                        f"🎯 *{c.name}* için yapmak istediğiniz işlemi seçin:",
                        action_buttons,
                    )
                    if not action:
                        return
                    dynamic_config["_action"] = action
                    dynamic_config["customer"] = c   # form auto-fill için

                    # 2. VFS giriş bilgileri — müşteri kaydından veya manuel
                    if c.vfs_email and c.vfs_password:
                        await query.message.reply_text(
                            f"✅ VFS giriş bilgileri müşteri kaydından alındı: `{c.vfs_email}`",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        dynamic_config["email"] = c.vfs_email
                        dynamic_config["password"] = c.vfs_password
                        dynamic_config["imap_password"] = ""
                    else:
                        email = await wait_input_cust("📧 *VFS hesap e-posta adresinizi girin:*")
                        if not email:
                            return
                        password = await wait_input_cust("🔑 *VFS hesap şifrenizi girin:*")
                        if not password:
                            return
                        dynamic_config["email"] = email.strip()
                        dynamic_config["password"] = password.strip()
                    await query.message.reply_text(
                        f"🔄 *{c.name}* için VFS'e bağlanılıyor...\n"
                        f"📋 Ad/soyad ve pasaport bilgileri müşteri kaydından otomatik doldurulacak.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    result = await fetch_site(
                        site, self.proxies, dynamic_config,
                        notify_func=notify_cust,
                        booking_mode=True,
                        wait_input_func=wait_input_cust,
                    )
                    if result["status_label"] == "BOOKED":
                        update_customer_status(cid, "randevu_alindi",
                                               appt_ref=result.get("matched_text", "")[:50],
                                               center=dynamic_config["center_text"])
                    await notify_cust(format_message(site, result, 0, dynamic_config))
                    # CLOSED sonucunda otomatik takip teklifi — session verisini sakla
                    if result["status_label"] == "CLOSED":
                        self._autobook_pending[chat_id] = {
                            "site_key":        site["key"],
                            "email":           dynamic_config.get("email", ""),
                            "password":        dynamic_config.get("password", ""),
                            "center_text":     dynamic_config.get("center_text", ""),
                            "category_text":   dynamic_config.get("category_text", ""),
                            "subcategory_text":dynamic_config.get("subcategory_text", ""),
                        }
                        await query.message.reply_text(
                            "💡 Slot bulunamadı. Aynı merkez/kategori kombinasyonu için "
                            "otomatik takip başlatmak ister misiniz?\n"
                            "_Slot açıldığında bildirim gelir — rezervasyon manuel yapılır._",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("👁 Otomatik Takip Başlat",
                                                     callback_data="autobook_from_session"),
                            ]]),
                        )
                except Exception as e:
                    logger.exception("Müşteri booking hatası")
                    await notify_cust(f"❌ Hata: {str(e)[:200]}")
                finally:
                    self._booking_queues.pop(chat_id, None)

            asyncio.create_task(_run_cust_booking())
            return

        if data.startswith("cust_status_menu:"):
            cid = int(data.split(":")[1])
            statuses = [
                ("⏳ Bekliyor",       "bekliyor"),
                ("✅ Randevu Alındı", "randevu_alindi"),
                ("🎉 Tamamlandı",    "tamamlandi"),
                ("❌ İptal",          "iptal"),
            ]
            rows = [
                [InlineKeyboardButton(label, callback_data=f"cust_set_status:{cid}:{val}")]
                for label, val in statuses
            ]
            rows.append([InlineKeyboardButton("◀ Geri", callback_data=f"cust_detail:{cid}"),
                         InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit("🚦 *Yeni durum seçin:*", InlineKeyboardMarkup(rows))
            return

        if data.startswith("cust_set_status:"):
            _, cid_str, new_status = data.split(":")
            cid = int(cid_str)
            update_customer_status(cid, new_status)
            c = get_customer(cid)
            await edit(
                f"✅ Durum güncellendi!\n\n{c.to_text()}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Detay", callback_data=f"cust_detail:{cid}"),
                     InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
                ]),
            )
            return

        # ── Mod 2: Otomatik Randevu Al (müşteri detayından, OTP-free tam otomasyon) ──
        if data.startswith("cust_autobook:"):
            cid = int(data.split(":")[1])
            c = get_customer(cid)
            if not c:
                await edit("❌ Müşteri bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            ab_active = c.auto_book_enabled and cid in self.auto_book_manager._tasks and not self.auto_book_manager._tasks[cid].done()
            if ab_active:
                msg = await self.auto_book_manager.stop_customer(cid)
                await edit(msg, InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀ Müşteri Detay", callback_data=f"cust_detail:{cid}")],
                ]))
            elif not c.vfs_email or not c.vfs_password:
                await edit(
                    f"❌ *{c.name}* için VFS e-posta/şifre kayıtlı değil.\n"
                    f"Müşteri kaydını güncelleyip tekrar dene.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("◀ Geri", callback_data=f"cust_detail:{cid}")]]),
                )
            else:
                # Site seçimi — destination'a göre filtrele
                dest_l = c.destination.lower() if c.destination else ""
                vfs_sites = [s for s in self.sites if s.get("provider", "").upper() == "VFS"
                             and (not dest_l or dest_l in s.get("name", "").lower()
                                  or s.get("name", "").lower() in dest_l)]
                if not vfs_sites:
                    vfs_sites = [s for s in self.sites if s.get("provider", "").upper() == "VFS"]
                seen_keys: set = set()
                site_btns = []
                for s in vfs_sites:
                    k = s["key"]
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    site_btns.append([InlineKeyboardButton(
                        s.get("menu_label", s.get("name", k)),
                        callback_data=f"cust_autobook_site:{cid}:{k}",
                    )])
                site_btns.append([InlineKeyboardButton("◀ Geri", callback_data=f"cust_detail:{cid}")])
                await edit(
                    f"🤖 *{c.name}* — Otomatik Randevu: Hangi site?\n"
                    f"_Slot bulunursa OTP gerektirmeyen ülkelerde otomatik rezervasyon yapılır._",
                    InlineKeyboardMarkup(site_btns),
                )
            return

        if data.startswith("cust_autobook_site:"):
            parts = data.split(":")
            cid, site_key = int(parts[1]), parts[2]
            c = get_customer(cid)
            if not c:
                return
            c.auto_book_site_key = site_key
            update_customer(c)
            msg = await self.auto_book_manager.start_customer(cid, notify_only=False)
            await edit(msg, InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ Müşteri Detay", callback_data=f"cust_detail:{cid}")],
            ]))
            return

        # ── Mod 1: Otomatik Takip (CLOSED sonucundan, sadece bildirim) ──
        # ── Flow 1: Session-based monitor (CLOSED sonucundan) ─────────────────
        if data == "autobook_from_session":
            chat_id = query.message.chat_id
            ctx = self._autobook_pending.get(chat_id)
            if not ctx:
                await query.message.reply_text("⚠️ Oturum süresi doldu. Yeniden kontrol yapın.")
                return
            site = next((s for s in self.sites if s["key"] == ctx["site_key"]), None)
            site_name = site.get("name", ctx["site_key"]) if site else ctx["site_key"]
            center_info = f"\n📍 Merkez: {ctx['center_text']}" if ctx.get("center_text") else ""
            cat_info    = f"\n📋 Kategori: {ctx['category_text']}" if ctx.get("category_text") else ""
            sub_info    = f"\n🗂 Alt Kategori: {ctx['subcategory_text']}" if ctx.get("subcategory_text") else ""
            msg = await self.auto_book_manager.start_session_monitor(chat_id, ctx)
            await query.message.reply_text(
                f"{msg}{center_info}{cat_info}{sub_info}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤖 Takip Paneli", callback_data="autobook_panel")],
                    [InlineKeyboardButton("⏹️ Durdur", callback_data=f"autobook_session_stop:{chat_id}")],
                    [InlineKeyboardButton("🏠 Ana Menü", callback_data="ana_menu")],
                ]),
            )
            self._autobook_pending.pop(chat_id, None)
            return

        if data == "autobook_session_stop":
            # Eski handler: parametresiz → kendi chat_id'si
            chat_id = query.message.chat_id
            msg = await self.auto_book_manager.stop_session_monitor(chat_id)
            await query.message.reply_text(
                f"{msg}\n\n{self._autobook_panel_text()}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self._autobook_panel_markup(chat_id),
            )
            return

        if data.startswith("autobook_pick_cust:"):
            # format: autobook_pick_cust:{site_key}:{default_cid}
            parts = data.split(":")
            site_key, default_cid = parts[1], int(parts[2])
            site = next((s for s in self.sites if s["key"] == site_key), None)
            site_name = site.get("name", site_key) if site else site_key
            # Kayıtlı müşterileri listele (VFS bilgisi olanları önce)
            all_custs = list_customers(limit=20)
            if not all_custs:
                await query.message.reply_text(
                    "❌ Kayıtlı müşteri yok. Önce /musteriler ile müşteri ekleyin.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            rows = []
            for cu in all_custs:
                has_creds = "✅" if cu.vfs_email else "❌"
                rows.append([InlineKeyboardButton(
                    f"{has_creds} {cu.to_text(short=True)}",
                    callback_data=f"autobook_start_notify:{cu.id}:{site_key}",
                )])
            rows.append([InlineKeyboardButton("❌ İptal", callback_data="ana_menu")])
            await query.message.reply_text(
                f"👁 *{site_name}* için otomatik takip — hangi müşteri için?\n"
                f"_✅ = VFS bilgisi kayıtlı, ❌ = eksik_",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        if data.startswith("autobook_start_notify:"):
            parts = data.split(":")
            cid, site_key = int(parts[1]), parts[2]
            c = get_customer(cid)
            if not c:
                await query.answer("Müşteri bulunamadı")
                return
            c.auto_book_site_key = site_key
            update_customer(c)
            msg = await self.auto_book_manager.start_customer(cid, notify_only=True)
            await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return

        if data.startswith("cust_delete_confirm:"):
            cid = int(data.split(":")[1])
            c = get_customer(cid)
            name = c.name if c else str(cid)
            await edit(
                f"⚠️ *{name}* silinsin mi?",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ Evet, Sil", callback_data=f"cust_delete:{cid}"),
                     InlineKeyboardButton("❌ İptal", callback_data=f"cust_detail:{cid}")],
                    [InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
                ]),
            )
            return

        if data.startswith("cust_delete:"):
            cid = int(data.split(":")[1])
            delete_customer(cid)
            await edit(
                "🗑️ Müşteri silindi.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Listeye Dön", callback_data="cust_list:all:0"),
                     InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
                ]),
            )
            return

        # ─────────────────────────────────────────────

        if data == "randevu_al":
            unique = []
            seen = set()
            for s in self.sites:
                g = s.get("group_key", s["key"])
                if g in seen:
                    continue
                seen.add(g)
                menu_label = s.get("menu_label") or s.get("name", "")
                flags = re.findall(r"[\U0001F1E0-\U0001F1FF]{2}", menu_label)
                name_only = re.sub(r"[\U0001F1E0-\U0001F1FF]", "", menu_label).strip()
                label = f"{flags[0]} {name_only}" if flags else name_only
                unique.append((g, label))
            rows = [
                [InlineKeyboardButton(label, callback_data=f"country:{g}") for g, label in unique[i:i+2]]
                for i in range(0, len(unique), 2)
            ]
            rows.append([InlineKeyboardButton("👤 Kayıtlı Müşteri Seç", callback_data="cust_pick_for_booking:0")])
            rows.append([InlineKeyboardButton("🏠 Ana Menü", callback_data="ana_menu")])
            await edit("🌍 Randevu alınacak ülkeyi seçin:", InlineKeyboardMarkup(rows), md=False)
            return

        if data.startswith("cust_pick_for_booking:"):
            offset = int(data.split(":")[1])
            customers = list_customers(status="bekliyor", limit=10, offset=offset)
            if not customers:
                customers = list_customers(limit=10, offset=offset)
            if not customers:
                await edit("📭 Kayıtlı müşteri yok. Önce müşteri ekleyin.", InlineKeyboardMarkup(back_menu), md=False)
                return
            rows = [
                [InlineKeyboardButton(c.to_text(short=True), callback_data=f"cust_selected_booking:{c.id}")]
                for c in customers
            ]
            nav = []
            if offset > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"cust_pick_for_booking:{offset-10}"))
            if len(customers) == 10:
                nav.append(InlineKeyboardButton("▶", callback_data=f"cust_pick_for_booking:{offset+10}"))
            if nav:
                rows.append(nav)
            rows.append([InlineKeyboardButton("◀ Geri", callback_data="randevu_al"),
                         InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit("👤 Randevu alınacak müşteriyi seçin:", InlineKeyboardMarkup(rows), md=False)
            return

        if data.startswith("cust_selected_booking:"):
            cid = int(data.split(":")[1])
            c = get_customer(cid)
            if not c:
                await edit("❌ Müşteri bulunamadı.", InlineKeyboardMarkup(back_menu))
                return
            # Ülke seçim ekranı — müşteri seçildi, şimdi ülke seç
            unique = []
            seen = set()
            for s in self.sites:
                g = s.get("group_key", s["key"])
                if g in seen:
                    continue
                seen.add(g)
                menu_label = s.get("menu_label") or s.get("name", "")
                flags = re.findall(r"[\U0001F1E0-\U0001F1FF]{2}", menu_label)
                name_only = re.sub(r"[\U0001F1E0-\U0001F1FF]", "", menu_label).strip()
                label = f"{flags[0]} {name_only}" if flags else name_only
                unique.append((g, label))
            rows = [
                [InlineKeyboardButton(label, callback_data=f"cust_book_site:{cid}:{g}") for g, label in unique[i:i+2]]
                for i in range(0, len(unique), 2)
            ]
            rows.append([InlineKeyboardButton("◀ Geri", callback_data="cust_pick_for_booking:0"),
                         InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit(f"👤 *{c.name}* için ülke seçin:", InlineKeyboardMarkup(rows))
            return

        if data == "istatistik":
            await edit(stats_summary(), InlineKeyboardMarkup(back_menu))
            return

        if data == "ana_menu" or data == "menu":
            await edit(
                self._customer_panel_text(),
                self._customer_panel_markup(),
            )
            return

        if data == "autobook_panel":
            chat_id = query.message.chat_id
            await edit(
                self._autobook_panel_text(),
                self._autobook_panel_markup(chat_id),
            )
            return

        if data.startswith("autobook_session_stop:"):
            sid = int(data.split(":")[1])
            msg = await self.auto_book_manager.stop_session_monitor(sid)
            chat_id = query.message.chat_id
            await edit(
                f"{msg}\n\n{self._autobook_panel_text()}",
                self._autobook_panel_markup(chat_id),
            )
            return

        if data.startswith("autobook_session_info:"):
            # Oturum detayı — şimdilik sadece durum
            sid = int(data.split(":")[1])
            active = self.auto_book_manager.session_monitor_active(sid)
            status_txt = "✅ Aktif çalışıyor" if active else "⭕ Durdu"
            await query.answer(f"Session {sid}: {status_txt}", show_alert=True)
            return

        if data.startswith("autobook_stop_cust:"):
            cid = int(data.split(":")[1])
            msg = await self.auto_book_manager.stop_customer(cid)
            chat_id = query.message.chat_id
            await edit(
                f"{msg}\n\n{self._autobook_panel_text()}",
                self._autobook_panel_markup(chat_id),
            )
            return

        if data.startswith("country:"):
            group_key = data.split(":", 1)[1]
            matched_sites = [s for s in self.sites if s.get("group_key", s["key"]) == group_key]

            if not matched_sites:
                await edit("Kayıt bulunamadı.", InlineKeyboardMarkup(back_menu), md=False)
                return

            first = matched_sites[0]

            if first.get("provider", "").upper() == "VFS":
                buttons = [
                    [InlineKeyboardButton("📅 Randevu Al (Canlı Merkez Seçimi)", callback_data=f"vfs_center:{first['key']}:live")],
                    [InlineKeyboardButton("📂 Mevcut Başvurularım", callback_data=f"vfs_my_bookings:{first['key']}")],
                    [InlineKeyboardButton("◀ Ülke Seçimine Dön", callback_data="randevu_al"),
                     InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")],
                ]
                await edit(
                    f"📍 *{first['name']}* — Giriş sonrası VFS'ten canlı merkez listesi gösterilecek.",
                    InlineKeyboardMarkup(buttons)
                )
                return

            for site in matched_sites:
                await query.message.reply_text(f"⏳ {site['center_name']} kontrol ediliyor...")
                result = await fetch_site(site, self.proxies, notify_func=query.message.reply_text)

                check_count = int(self.state.get(site["key"], {}).get("check_count", 0)) + 1
                self.state[site["key"]] = {
                    "status_label": result["status_label"],
                    "check_count": check_count,
                    "matched_text": result["matched_text"],
                    "signature": result["signature"],
                }
                save_state(self.state)

                await query.message.reply_text(
                    format_message(site, result, check_count),
                    parse_mode=ParseMode.MARKDOWN,
                )
            return

        if data.startswith("vfs_center:"):
            _, site_key, center_idx = data.split(":")
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site:
                return

            # "live" → giriş sonrası VFS'ten canlı merkez seçimi
            if center_idx == "live":
                center_text = ""
            else:
                center_text = site["vfs_options"]["centers"][int(center_idx)]
            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await query.message.reply_text("⚠️ Zaten aktif bir başvuru akışı var. Lütfen bekleyin.")
                return

            # Kategori ve alt kategori login sonrası VFS'ten dinamik çekilecek
            dynamic_config_vc = {"center_text": center_text}
            q_vc: asyncio.Queue = asyncio.Queue()
            self._booking_queues[chat_id] = q_vc

            async def wait_input_vc(prompt: str, buttons=None):
                kwargs = {"parse_mode": ParseMode.MARKDOWN}
                if buttons:
                    kwargs["reply_markup"] = InlineKeyboardMarkup(buttons)
                sent_msg = await query.message.reply_text(prompt, **kwargs)
                try:
                    value = await asyncio.wait_for(q_vc.get(), timeout=300)
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    return value
                except asyncio.TimeoutError:
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    await query.message.reply_text("⏱ Zaman aşımı — iptal edildi.")
                    return None

            async def notify_vc(msg: str):
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            async def _run_vc_task():
                try:
                    action_buttons = [
                        [InlineKeyboardButton("📅 Yeni Randevu Al",           callback_data="vfs_pick:__yeni__")],
                        [InlineKeyboardButton("📂 Mevcut Başvurularımı Gör", callback_data="vfs_pick:__sorgula__")],
                    ]
                    _center_line = f"📍 *{center_text}*\n\n" if center_text else "🏢 Merkez giriş sonrası VFS'ten seçilecek\n\n"
                    action = await wait_input_vc(
                        f"{_center_line}🎯 Yapmak istediğiniz işlemi seçin:\n_(Seçim sonrası giriş bilgileri istenecek)_",
                        action_buttons,
                    )
                    if not action:
                        return
                    dynamic_config_vc["_action"] = action

                    email = await wait_input_vc("📧 *VFS hesap e-posta adresinizi girin:*")
                    if not email:
                        return
                    password = await wait_input_vc("🔑 *VFS hesap şifrenizi girin:*")
                    if not password:
                        return
                    dynamic_config_vc["email"] = email.strip()
                    dynamic_config_vc["password"] = password.strip()
                    await query.message.reply_text("🔄 Bilgiler alındı, VFS'e bağlanılıyor...", parse_mode=ParseMode.MARKDOWN)
                    result = await fetch_site(
                        site, self.proxies, dynamic_config_vc,
                        notify_func=notify_vc,
                        booking_mode=True,
                        wait_input_func=wait_input_vc,
                    )
                    check_count = int(self.state.get(site["key"], {}).get("check_count", 0)) + 1
                    self.state[site["key"]] = {
                        "status_label": result["status_label"],
                        "check_count": check_count,
                        "matched_text": result["matched_text"],
                        "signature": result["signature"],
                    }
                    save_state(self.state)
                    await notify_vc(format_message(site, result, check_count, dynamic_config_vc))
                except Exception as e:
                    logger.exception("vfs_center booking hatası")
                    await notify_vc(f"❌ Hata: {str(e)[:200]}")
                finally:
                    self._booking_queues.pop(chat_id, None)

            asyncio.create_task(_run_vc_task())
            return

        if data.startswith("vfs_category:"):
            _, site_key, center_idx, cat_idx = data.split(":")
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site:
                return

            cat_text = site["vfs_options"]["categories"][int(cat_idx)]
            cat_sub_map = site["vfs_options"].get("category_subcategory_map", {})
            if cat_sub_map and cat_text in cat_sub_map:
                subcategories = cat_sub_map[cat_text]
            else:
                subcategories = site["vfs_options"].get("subcategories", [])

            buttons = [
                [InlineKeyboardButton(sub[:48], callback_data=f"vfs_sub:{site_key}:{center_idx}:{cat_idx}:{i}")]
                for i, sub in enumerate(subcategories)
            ]
            buttons.append([InlineKeyboardButton("◀ Geri", callback_data=f"vfs_center:{site_key}:{center_idx}"),
                             InlineKeyboardButton("🏠 Menü", callback_data="ana_menu")])
            await edit("📄 Lütfen Alt Kategoriyi Seçin:", InlineKeyboardMarkup(buttons), md=False)
            return

        if data.startswith("vfs_sub:"):
            _, site_key, center_idx, cat_idx, sub_idx = data.split(":")
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site:
                return

            cat_text = site["vfs_options"]["categories"][int(cat_idx)]
            cat_sub_map = site["vfs_options"].get("category_subcategory_map", {})
            if cat_sub_map and cat_text in cat_sub_map:
                subcategories = cat_sub_map[cat_text]
            else:
                subcategories = site["vfs_options"].get("subcategories", [])

            dynamic_config = {
                "center_text": site["vfs_options"]["centers"][int(center_idx)],
                "category_text": cat_text,
                "subcategory_text": subcategories[int(sub_idx)],
            }

            # CLOSED cooldown kontrolü — son sorgu CLOSED ise bekleme süresi dolmadan yeni istek atma
            site_state = self.state.get(site["key"], {})
            if site_state.get("status_label") == "CLOSED":
                last_ts = site_state.get("last_closed_ts", 0)
                elapsed = time.time() - last_ts
                remaining = CLOSED_COOLDOWN_SECS - elapsed
                if remaining > 0:
                    mins_elapsed = int(elapsed // 60)
                    mins_remaining = int(remaining // 60) + 1
                    await query.message.reply_text(
                        f"⏳ Son kontrol {mins_elapsed} dakika önce yapıldı — sonuç *KAPALI*'ydı.\n"
                        f"Yeni sorgu için *{mins_remaining} dakika* daha bekleyin.",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

            await query.message.reply_text(f"⏳ {site['name']} için sorgu yapılıyor...")

            result = await fetch_site(site, self.proxies, dynamic_config, notify_func=query.message.reply_text)
            check_count = int(self.state.get(site["key"], {}).get("check_count", 0)) + 1
            new_state = {
                "status_label": result["status_label"],
                "check_count": check_count,
                "matched_text": result["matched_text"],
                "signature": result["signature"],
            }
            if result["status_label"] == "CLOSED":
                new_state["last_closed_ts"] = time.time()
            self.state[site["key"]] = new_state
            save_state(self.state)

            msg_text = format_message(site, result, check_count, dynamic_config)
            if result["status_label"] == "PENDING":
                book_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "📋 Başvuruya Devam Et",
                        callback_data=f"vfs_book_start:{site_key}:{center_idx}:{cat_idx}:{sub_idx}"
                    )
                ]])
                await query.message.reply_text(msg_text, parse_mode=ParseMode.MARKDOWN, reply_markup=book_markup)
            else:
                await query.message.reply_text(msg_text, parse_mode=ParseMode.MARKDOWN)
            return

        if data.startswith("vfs_pick:"):
            value = data.split(":", 1)[1]
            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await self._booking_queues[chat_id].put(value)
            await query.answer(f"✅ Seçildi: {value}")
            return

        if data.startswith("vfs_book_start:"):
            _, site_key, center_idx, cat_idx, sub_idx = data.split(":")
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site:
                return

            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await query.message.reply_text("⚠️ Zaten aktif bir başvuru akışı var. Lütfen bekleyin.")
                return

            cat_text = site["vfs_options"]["categories"][int(cat_idx)]
            cat_sub_map = site["vfs_options"].get("category_subcategory_map", {})
            subcategories = cat_sub_map.get(cat_text) or site["vfs_options"].get("subcategories", [])
            dynamic_config = {
                "center_text": site["vfs_options"]["centers"][int(center_idx)],
                "category_text": cat_text,
                "subcategory_text": subcategories[int(sub_idx)],
            }

            q: asyncio.Queue = asyncio.Queue()
            self._booking_queues[chat_id] = q

            async def wait_input_func(prompt: str, buttons=None):
                kwargs = {"parse_mode": ParseMode.MARKDOWN}
                if buttons:
                    kwargs["reply_markup"] = InlineKeyboardMarkup(buttons)
                sent_msg = await query.message.reply_text(prompt, **kwargs)
                try:
                    value = await asyncio.wait_for(q.get(), timeout=300)
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    return value
                except asyncio.TimeoutError:
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    await query.message.reply_text("⏱ Zaman aşımı — başvuru iptal edildi.")
                    return None

            async def notify_booking(msg: str):
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            async def _run_booking_task():
                try:
                    # 1. Login öncesi işlem seçimi
                    action_buttons = [
                        [InlineKeyboardButton("📅 Yeni Randevu Al",           callback_data="vfs_pick:__yeni__")],
                        [InlineKeyboardButton("📂 Mevcut Başvurularımı Gör", callback_data="vfs_pick:__sorgula__")],
                    ]
                    action = await wait_input_func(
                        "🎯 *Yapmak istediğiniz işlemi seçin:*\n_(Seçim sonrası giriş bilgileri istenecek)_",
                        action_buttons,
                    )
                    if not action:
                        return
                    dynamic_config["_action"] = action

                    # 2. VFS giriş bilgileri
                    email = await wait_input_func("📧 *VFS hesap e-posta adresinizi girin:*")
                    if not email:
                        return
                    password = await wait_input_func("🔑 *VFS hesap şifrenizi girin:*")
                    if not password:
                        return
                    dynamic_config["email"] = email.strip()
                    dynamic_config["password"] = password.strip()
                    await query.message.reply_text(
                        "🔄 Bilgiler alındı, VFS'e bağlanılıyor...",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    result = await fetch_site(
                        site, self.proxies, dynamic_config,
                        notify_func=notify_booking,
                        booking_mode=True,
                        wait_input_func=wait_input_func,
                    )
                    check_count = int(self.state.get(site["key"], {}).get("check_count", 0)) + 1
                    self.state[site["key"]] = {
                        "status_label": result["status_label"],
                        "check_count": check_count,
                        "matched_text": result["matched_text"],
                        "signature": result["signature"],
                    }
                    save_state(self.state)
                    await notify_booking(format_message(site, result, check_count, dynamic_config))
                except Exception as e:
                    logger.exception("Booking task hatası")
                    await notify_booking(f"❌ Başvuru hatası: {str(e)[:200]}")
                finally:
                    self._booking_queues.pop(chat_id, None)

            asyncio.create_task(_run_booking_task())
            return

        if data.startswith("vfs_my_bookings:"):
            site_key = data.split(":", 1)[1]
            site = next((s for s in self.sites if s["key"] == site_key), None)
            if not site:
                return

            chat_id = query.message.chat_id
            if chat_id in self._booking_queues:
                await query.message.reply_text("⚠️ Zaten aktif bir akış var. Lütfen bekleyin.")
                return

            q: asyncio.Queue = asyncio.Queue()
            self._booking_queues[chat_id] = q

            async def wait_input_resume(prompt: str, buttons=None):
                kwargs = {"parse_mode": ParseMode.MARKDOWN}
                if buttons:
                    kwargs["reply_markup"] = InlineKeyboardMarkup(buttons)
                sent_msg = await query.message.reply_text(prompt, **kwargs)
                try:
                    value = await asyncio.wait_for(q.get(), timeout=300)
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    return value
                except asyncio.TimeoutError:
                    if buttons:
                        try:
                            await sent_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup([]))
                        except Exception:
                            pass
                    await query.message.reply_text("⏱ Zaman aşımı.")
                    return None

            async def notify_resume(msg: str):
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            async def _run_resume_task():
                try:
                    # VFS giriş bilgileri (bu akış zaten "sorgula" — işlem sabit)
                    email = await wait_input_resume("📧 *VFS hesap e-posta adresinizi girin:*")
                    if not email:
                        return
                    password = await wait_input_resume("🔑 *VFS hesap şifrenizi girin:*")
                    if not password:
                        return
                    cred_config = {
                        "email": email.strip(),
                        "password": password.strip(),
                        "_action": "__sorgula__",
                    }
                    await query.message.reply_text(
                        "🔄 Bilgiler alındı, VFS'e bağlanılıyor...",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    result = await fetch_site(
                        site, self.proxies, cred_config,
                        notify_func=notify_resume,
                        resume_mode=True,
                        wait_input_func=wait_input_resume,
                    )
                    if result["status_label"] == "PAYMENT":
                        pay_link = result["matched_text"]
                        pay_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💳 Ödeme Yap", url=pay_link)
                        ]])
                        await query.message.reply_text(
                            f"💳 *Ödeme Linki Hazır!*\nAşağıdaki butona basarak ödemeyi tamamlayın:",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=pay_markup,
                        )
                    else:
                        await notify_resume(format_message(site, result, 0))
                except Exception as e:
                    logger.exception("Resume task hatası")
                    await notify_resume(f"❌ Hata: {str(e)[:200]}")
                finally:
                    self._booking_queues.pop(chat_id, None)

            asyncio.create_task(_run_resume_task())
            return

    async def _notify_admin_text(self, msg: str) -> None:
        """AutoBookManager'ın admin bildirimleri için callback."""
        try:
            await self.app.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Admin bildirim hatası: {e}")

    async def run(self) -> None:
        _cleanup_stale_gost_tunnels()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        # Bot başladığında DB'de aktif auto-booking görevlerini restore et
        restored = await self.auto_book_manager.restore_active()
        if restored:
            logger.info(f"Auto-booking: {restored} müşteri takibi restore edildi")
            await self._notify_admin_text(
                f"🤖 Bot yeniden başlatıldı — {restored} aktif otomatik takip devam ediyor."
            )
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    service = VisaWatchService()
    asyncio.run(service.run())
