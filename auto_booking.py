#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Otomatik randevu takip ve rezervasyon modülü.

Kayıtlı müşteriler için arka planda periyodik slot kontrolü yapar.
OTP gerektirmeyen ülkelerde slot bulunduğunda otomatik rezervasyon tamamlar.

Kullanım:
    manager = AutoBookManager(sites, proxies, notify_admin_func)
    await manager.start_customer(customer_id)   # Takibi başlat
    await manager.stop_customer(customer_id)    # Takibi durdur
    manager.status(customer_id)                 # Durum sorgula
"""

import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional

from customer_db import (
    Customer, get_customer, update_customer,
    update_customer_status, list_customers,
)

logger = logging.getLogger(__name__)

# ─── OTP Gerektirmeyen Ülkeler ────────────────────────────────────────────────
# Kullanıcı listesi teslim ettikçe buraya ekle (site["key"] değerleri)
# Örnek: {"vfs_dk", "vfs_nl", "vfs_se"}
OTP_FREE_SITE_KEYS: set[str] = set()
# TODO: Kullanıcıdan liste alındıktan sonra doldur

# ─── Sabitler ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SECS = 2 * 3600        # 2 saatte bir kontrol
TASK_TIMEOUT_SECS   = 10 * 60        # Tek bir check'in maksimum süresi (10 dk)
STATUS_WAITING      = "waiting"       # Kontrol bekleniyor
STATUS_CHECKING     = "checking"      # Şu an kontrol ediliyor
STATUS_BOOKED       = "booked"        # Rezervasyon tamamlandı
STATUS_ERROR        = "error"         # Hata (detay auto_book_status'ta)
STATUS_PAUSED       = "paused"        # Manuel durduruldu


class AutoBookManager:
    """Kayıtlı müşteriler için arka plan otomatik takip yöneticisi."""

    def __init__(
        self,
        sites: list[dict],
        proxies: list[dict],
        notify_admin: Callable[[str], Awaitable[None]],
    ):
        self.sites = sites
        self.proxies = proxies
        self.notify_admin = notify_admin
        # customer_id → asyncio.Task  (Flow 2: kayıtlı müşteri, tam otomasyon)
        self._tasks: dict[int, asyncio.Task] = {}
        # chat_id → asyncio.Task  (Flow 1: session tabanlı, sadece bildirim)
        self._session_tasks: dict[int, asyncio.Task] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    async def start_customer(self, customer_id: int, notify_only: bool = False) -> str:
        """
        Müşteri için otomatik takibi başlatır.

        notify_only=True  → Mod 1: Slot bulunca bildirim gönder, rezervasyon yapma.
                            CLOSED sonuçtan "Otomatik Takip Başlat" ile tetiklenir.
        notify_only=False → Mod 2: Slot bulunca otomatik rezervasyon tamamla.
                            OTP-free ülkelerde müşteri detayından "Otomatik Randevu Al" ile tetiklenir.

        Dönüş: açıklama mesajı (Telegram'a iletilir)
        """
        if customer_id in self._tasks and not self._tasks[customer_id].done():
            return "⚠️ Bu müşteri için zaten aktif bir takip var."

        c = get_customer(customer_id)
        if not c:
            return "❌ Müşteri bulunamadı."
        if not c.vfs_email or not c.vfs_password:
            return (
                f"❌ *{c.name}* için VFS e-posta/şifre kayıtlı değil.\n"
                f"Müşteri kaydını güncelleyip tekrar dene."
            )
        if not c.auto_book_site_key:
            return "❌ Hangi ülke/site takip edilecek? Önce site seçilmeli."

        site = self._find_site(c.auto_book_site_key)
        if not site:
            return f"❌ Site bulunamadı: `{c.auto_book_site_key}`"

        mode = "notify" if notify_only else "auto"

        if not notify_only and c.auto_book_site_key not in OTP_FREE_SITE_KEYS:
            return (
                f"⚠️ *{site.get('name', c.auto_book_site_key)}* OTP gerektiriyor.\n"
                f"Bu ülkede otomatik rezervasyon henüz desteklenmiyor.\n"
                f"Sadece bildirim modu için /musteriler → müşteri → Otomatik Takip Başlat kullanın."
            )

        if not notify_only and not c.visa_type:
            return (
                f"❌ *{c.name}* için vize tipi (alt kategori) kayıtlı değil.\n"
                f"Müşteri kaydında *Vize Tipi* alanını doldurun."
            )

        # DB'yi güncelle
        c.auto_book_enabled  = 1
        c.auto_book_mode     = mode
        c.auto_book_status   = STATUS_WAITING
        update_customer(c)

        task = asyncio.create_task(
            self._monitor_loop(customer_id),
            name=f"autobook_{customer_id}",
        )
        self._tasks[customer_id] = task
        logger.info(
            f"Otomatik takip başlatıldı [{mode}]: müşteri #{customer_id} "
            f"({c.name}) → {c.auto_book_site_key}"
        )

        if notify_only:
            return (
                f"👁 *{c.name}* için otomatik takip başladı.\n"
                f"🌍 Site: `{c.auto_book_site_key}`\n"
                f"📋 Vize: {c.visa_type or '(tümü)'}\n"
                f"⏰ Her 2 saatte bir kontrol, slot açılınca bildirim gelir."
            )
        else:
            return (
                f"🤖 *{c.name}* için otomatik randevu modu başladı.\n"
                f"🌍 Site: `{c.auto_book_site_key}`\n"
                f"📋 Vize: {c.visa_type}\n"
                f"⏰ Her 2 saatte bir kontrol, slot bulunursa otomatik rezervasyon yapılır."
            )

    async def stop_customer(self, customer_id: int) -> str:
        """Müşteri için otomatik takibi durdurur."""
        task = self._tasks.pop(customer_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        c = get_customer(customer_id)
        if c:
            c.auto_book_enabled = 0
            c.auto_book_status  = STATUS_PAUSED
            update_customer(c)
            logger.info(f"Otomatik takip durduruldu: müşteri #{customer_id} ({c.name})")
            return f"⏸️ *{c.name}* için otomatik takip durduruldu."
        return "⏸️ Takip durduruldu."

    def status(self, customer_id: int) -> str:
        """Mevcut takip durumunu döndürür."""
        task = self._tasks.get(customer_id)
        c = get_customer(customer_id)
        db_status = c.auto_book_status if c else ""

        if task and not task.done():
            emoji = "🔄" if db_status == STATUS_CHECKING else "⏳"
            return f"{emoji} Aktif ({db_status})"
        if db_status == STATUS_BOOKED:
            return "✅ Rezervasyon tamamlandı"
        if db_status == STATUS_PAUSED:
            return "⏸️ Durduruldu"
        if db_status == STATUS_ERROR:
            return "❌ Hata"
        return "⭕ Pasif"

    async def restore_active(self) -> int:
        """
        Bot yeniden başlatıldığında DB'de auto_book_enabled=1 olan
        müşterilerin takibini otomatik sürdürür.
        Dönüş: kaç müşteri restore edildi
        """
        restored = 0
        for c in list_customers():
            if c.auto_book_enabled and c.auto_book_site_key:
                if c.id not in self._tasks or self._tasks[c.id].done():
                    # Doğrudan task başlat (msg dönüşü gerekmez)
                    task = asyncio.create_task(
                        self._monitor_loop(c.id),
                        name=f"autobook_{c.id}",
                    )
                    self._tasks[c.id] = task
                    restored += 1
                    logger.info(f"Restore: müşteri #{c.id} ({c.name}) takip devam ediyor")
        return restored

    def active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    # ──────────────────────────────────────────────────────────────────────────
    # Flow 1: Session-based monitor (CLOSED sonucundan — sadece bildirim)
    # ──────────────────────────────────────────────────────────────────────────

    async def start_session_monitor(self, chat_id: int, ctx: dict) -> str:
        """
        Manuel kontrol → CLOSED sonucu → "Otomatik Takip Başlat".

        ctx = {
            site_key, email, password,
            center_text, category_text, subcategory_text  (opsiyonel)
        }

        Her 2 saatte bir aynı kombinasyonu kontrol eder.
        Slot açılınca admin'e bildirim gönderir, rezervasyon yapmaz.
        """
        # Önce varsa eski görevi durdur
        old = self._session_tasks.pop(chat_id, None)
        if old and not old.done():
            old.cancel()

        site_key = ctx.get("site_key", "")
        if not site_key:
            return "❌ Site bilgisi eksik."
        site = self._find_site(site_key)
        if not site:
            return f"❌ Site bulunamadı: `{site_key}`"
        if not ctx.get("email") or not ctx.get("password"):
            return "❌ VFS giriş bilgileri (e-posta/şifre) eksik."

        task = asyncio.create_task(
            self._session_monitor_loop(chat_id, ctx),
            name=f"session_monitor_{chat_id}",
        )
        self._session_tasks[chat_id] = task
        logger.info(
            f"Session monitor başlatıldı: chat_id={chat_id} site={site_key} "
            f"merkez={ctx.get('center_text','?')!r}"
        )
        site_name = site.get("name", site_key)
        return (
            f"👁 *{site_name}* için otomatik takip başladı.\n"
            f"⏰ Her 2 saatte bir kontrol, slot açılınca bildirim gelir.\n"
            f"Durdurmak için menüden /stop\\_monitor veya tekrar kontrol yapın."
        )

    async def stop_session_monitor(self, chat_id: int) -> str:
        task = self._session_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return "⏸️ Otomatik takip durduruldu."
        return "ℹ️ Aktif session takibi yok."

    def session_monitor_active(self, chat_id: int) -> bool:
        t = self._session_tasks.get(chat_id)
        return t is not None and not t.done()

    async def _session_monitor_loop(self, chat_id: int, ctx: dict) -> None:
        """2 saatte bir aynı kombinasyonu kontrol et, slot açılınca bildir."""
        from visa_watch_onefile import fetch_site

        site_key = ctx["site_key"]
        site = self._find_site(site_key)
        if not site:
            return

        dynamic_config = {
            "email":            ctx["email"],
            "password":         ctx["password"],
            "imap_password":    "",
            "center_text":      ctx.get("center_text", ""),
            "category_text":    ctx.get("category_text", ""),
            "subcategory_text": ctx.get("subcategory_text", ""),
        }
        center_label = ctx.get("center_text") or "herhangi bir merkez"
        cat_label    = ctx.get("subcategory_text") or ctx.get("category_text") or ""

        _tried_centers: list[str] = []

        async def _auto_input(prompt: str, buttons=None) -> Optional[str]:
            p = prompt.lower()
            if "işlem" in p or "yapmak istediğiniz" in p:
                return "__yeni__"
            if "merkez" in p and buttons:
                opts = [
                    btn.callback_data.replace("vfs_pick:", "")
                    for row in (buttons or []) for btn in row
                    if hasattr(btn, "callback_data")
                    and btn.callback_data.startswith("vfs_pick:")
                    and not btn.callback_data.replace("vfs_pick:", "").startswith("__")
                ]
                for opt in opts:
                    if opt not in _tried_centers:
                        _tried_centers.append(opt)
                        return opt
                return "__cancel__"
            # Slot bulundu → sadece bildir, rezervasyon yapma
            if "__confirm_book__" in str(buttons):
                return "__cancel__"
            if "__retry_center__" in str(buttons):
                all_opts = [
                    btn.callback_data.replace("vfs_pick:", "")
                    for row in (buttons or []) for btn in row
                    if hasattr(btn, "callback_data")
                    and btn.callback_data.startswith("vfs_pick:")
                    and not btn.callback_data.replace("vfs_pick:", "").startswith("__")
                ]
                untried = [o for o in all_opts if o not in _tried_centers]
                return "__retry_center__" if untried else "__cancel__"
            if "otp" in p or "6 haneli" in p:
                return None
            return None

        while True:
            _tried_centers.clear()
            try:
                result = await asyncio.wait_for(
                    fetch_site(
                        site, self.proxies, dynamic_config,
                        notify_func=None,
                        booking_mode=True,
                        wait_input_func=_auto_input,
                    ),
                    timeout=TASK_TIMEOUT_SECS,
                )
                status = result.get("status_label", "")
                matched = result.get("matched_text", "")

                if status not in ("CLOSED", "CANCELLED", "ERROR"):
                    # Slot var — bildir
                    await self.notify_admin(
                        f"📅 *SLOT AÇILDI — Manuel Randevu Gerekiyor*\n"
                        f"🌍 Site: `{site_key}`\n"
                        f"📍 Merkez: {center_label}\n"
                        f"✈️ Vize: {cat_label}\n"
                        f"📋 {matched}\n\n"
                        f"_Hemen randevu almak için VFS'e gidin._"
                    )
                else:
                    logger.info(f"Session monitor [{chat_id}]: {status} — {matched[:60]}")

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(f"Session monitor [{chat_id}]: zaman aşımı")
            except Exception as e:
                logger.error(f"Session monitor [{chat_id}]: hata: {e}")

            await asyncio.sleep(CHECK_INTERVAL_SECS)

    # ──────────────────────────────────────────────────────────────────────────
    # Arka plan döngüsü
    # ──────────────────────────────────────────────────────────────────────────

    async def _monitor_loop(self, customer_id: int) -> None:
        """2 saatte bir slot kontrolü yapar; slot bulunursa rezervasyon dener."""
        while True:
            c = get_customer(customer_id)
            if not c or not c.auto_book_enabled:
                logger.info(f"Takip iptal edildi (DB kapalı): #{customer_id}")
                return

            # Durum: checking
            c.auto_book_status    = STATUS_CHECKING
            c.auto_book_last_check = time.time()
            update_customer(c)

            try:
                result = await asyncio.wait_for(
                    self._check_and_book(c),
                    timeout=TASK_TIMEOUT_SECS,
                )
                await self._handle_result(c, result)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(f"Auto-check zaman aşımı: #{customer_id}")
                c = get_customer(customer_id)
                if c:
                    c.auto_book_status = STATUS_ERROR + ":timeout"
                    update_customer(c)
                await self.notify_admin(
                    f"⚠️ Auto-check zaman aşımı: *{c.name if c else customer_id}*"
                )
            except Exception as e:
                logger.exception(f"Auto-check hatası: #{customer_id}: {e}")
                c = get_customer(customer_id)
                if c:
                    c.auto_book_status = f"{STATUS_ERROR}:{str(e)[:60]}"
                    update_customer(c)

            # Bir sonraki kontrole kadar bekle
            c = get_customer(customer_id)
            if not c or not c.auto_book_enabled or c.auto_book_status.startswith(STATUS_BOOKED):
                return
            logger.info(f"Sonraki kontrol {CHECK_INTERVAL_SECS//3600}s sonra: #{customer_id}")
            await asyncio.sleep(CHECK_INTERVAL_SECS)

    async def _check_and_book(self, c: Customer) -> dict:
        """
        Slot kontrolü + varsa rezervasyon.

        OTP gerektirmeyen ülkelerde (OTP_FREE_SITE_KEYS) slot bulunursa
        otomatik rezervasyon tamamlanır.

        OTP gerektiren ülkelerde sadece bildirim yapılır.
        """
        # Yerel import (circular import önlemek için)
        from visa_watch_onefile import fetch_site

        site = self._find_site(c.auto_book_site_key)
        if not site:
            raise ValueError(f"Site bulunamadı: {c.auto_book_site_key}")

        notify_only = (c.auto_book_mode == "notify")
        otp_free    = (c.auto_book_site_key in OTP_FREE_SITE_KEYS)
        # Mod 1 (notify): slot bulunca bildir, rezervasyon yapma
        # Mod 2 (auto):   slot bulunca otomatik rezervasyon (sadece OTP-free)

        dynamic_config = {
            "email":            c.vfs_email,
            "password":         c.vfs_password,
            "imap_password":    "",            # IMAP kullanılmıyor
            "subcategory_text": c.visa_type,   # Wizard'dan kaydedilen vize tipi
            "category_text":    "",            # API'den otomatik eşleşecek
        }

        # Denenen merkezleri kaydet (retry loop ile tüm merkezler taranır)
        _tried_centers: list[str] = []
        _booking_result: dict = {}
        _notify_log: list[str] = []

        async def _notify(msg: str) -> None:
            _notify_log.append(msg)
            logger.info(f"[auto #{c.id}] {msg}")

        async def _auto_input(prompt: str, buttons=None) -> Optional[str]:
            """
            Otomatik yanıtlayıcı — Telegram kullanıcı girişini simüle eder.
            Her prompt türünü müşteri verisiyle veya otomatik seçimle karşılar.
            """
            p = prompt.lower()

            # 1. İşlem seçimi
            if "işlem" in p or "yapmak istediğiniz" in p:
                return "__yeni__"

            # 2. Merkez seçimi — ilk denenmemiş merkezi seç
            if "merkez" in p and buttons:
                opts = [
                    btn.callback_data.replace("vfs_pick:", "")
                    for row in (buttons or [])
                    for btn in row
                    if hasattr(btn, "callback_data")
                    and btn.callback_data.startswith("vfs_pick:")
                    and not btn.callback_data.replace("vfs_pick:", "").startswith("__")
                ]
                for opt in opts:
                    if opt not in _tried_centers:
                        _tried_centers.append(opt)
                        logger.info(f"[auto #{c.id}] Merkez seçildi: {opt!r}")
                        return opt
                logger.info(f"[auto #{c.id}] Tüm merkezler denendi: {_tried_centers}")
                return "__cancel__"

            # 3. Slot bulundu — onayla veya başka merkez
            if "__confirm_book__" in str(buttons):
                if notify_only:
                    # Mod 1: sadece bildirim, rezervasyon istenmiyor
                    logger.info(f"[auto #{c.id}] Slot bulundu (notify modu) → iptal, bildirim gönderilecek")
                    _booking_result["slot_found_notify"] = True
                    return "__cancel__"
                elif otp_free:
                    logger.info(f"[auto #{c.id}] Slot bulundu, OTP-free → onaylanıyor")
                    return "__confirm_book__"
                else:
                    # OTP gerektiren ülke — sadece bildirim
                    logger.info(f"[auto #{c.id}] Slot bulundu, OTP gerekli → sadece bildir")
                    _booking_result["slot_found_no_otp"] = True
                    return "__cancel__"

            # 4. Başka merkez kontrolü (retry loop)
            if "__retry_center__" in str(buttons):
                # Henüz denenmemiş merkez varsa retry et
                all_opts = [
                    btn.callback_data.replace("vfs_pick:", "")
                    for row in (buttons or [])
                    for btn in row
                    if hasattr(btn, "callback_data")
                    and btn.callback_data.startswith("vfs_pick:")
                    and not btn.callback_data.replace("vfs_pick:", "").startswith("__")
                ]
                untried = [o for o in all_opts if o not in _tried_centers]
                if untried:
                    return "__retry_center__"
                return "__cancel__"

            # 5. OTP alanı
            if "otp" in p or "6 haneli" in p:
                if otp_free:
                    # TODO: OTP_FREE_SITE_KEYS doğrulandıktan sonra
                    # bu ülkede OTP gelmemeli — geldiyse hata
                    logger.error(f"[auto #{c.id}] OTP-free ülkede OTP istendi — beklenmedik!")
                    return None
                else:
                    # OTP gerekiyor, otomatik çözemeyiz
                    logger.info(f"[auto #{c.id}] OTP gerekli — otomatik devam edilemiyor")
                    return None

            # 6. Bilinmeyen prompt — logla, None döndür (timeout/cancel)
            logger.warning(f"[auto #{c.id}] Bilinmeyen prompt: {prompt[:100]!r}")
            return None

        # fetch_site çağrısı — booking_mode=True, otomatik yanıtlayıcı ile
        result = await fetch_site(
            site,
            self.proxies,
            dynamic_config,
            notify_func=_notify,
            booking_mode=True,
            wait_input_func=_auto_input,
        )

        # Slot bulundu ama OTP nedeniyle rezervasyon yapılamadıysa işaretle
        if _booking_result.get("slot_found_no_otp"):
            result["_slot_found_no_otp"] = True

        return result

    async def _handle_result(self, c: Customer, result: dict) -> None:
        """Kontrol sonucunu işle, gerekirse bildirim gönder ve DB güncelle."""
        status_label = result.get("status_label", "")
        matched_text = result.get("matched_text", "")

        if result.get("_slot_found_notify"):
            # Mod 1: Slot var, bildirim gönder (rezervasyon istenmiyor)
            msg = (
                f"📅 *RANDEVU SLOTU AÇILDI!*\n"
                f"👤 Müşteri: *{c.name}*\n"
                f"🌍 Site: `{c.auto_book_site_key}`\n"
                f"✈️ Vize: {c.visa_type}\n"
                f"📋 {matched_text}\n\n"
                f"_Manuel randevu almak için /musteriler → müşteriyi seç → Randevu Al_"
            )
            await self.notify_admin(msg)
            c2 = get_customer(c.id)
            if c2:
                c2.auto_book_status = STATUS_WAITING
                update_customer(c2)

        elif result.get("_slot_found_no_otp"):
            # Mod 2 ama OTP gerekli: bildirim gönder
            msg = (
                f"📅 *SLOT BULUNDU* (OTP gerekli — manuel işlem gerekiyor)\n"
                f"👤 Müşteri: *{c.name}*\n"
                f"🌍 Site: `{c.auto_book_site_key}`\n"
                f"✈️ Vize: {c.visa_type}\n"
                f"📋 {matched_text}"
            )
            await self.notify_admin(msg)
            c2 = get_customer(c.id)
            if c2:
                c2.auto_book_status = STATUS_WAITING
                update_customer(c2)

        elif status_label == "BOOKED":
            # Rezervasyon tamamlandı!
            msg = (
                f"🎉 *OTOMATİK REZERVASYON TAMAMLANDI!*\n"
                f"👤 Müşteri: *{c.name}*\n"
                f"🌍 Site: `{c.auto_book_site_key}`\n"
                f"📋 {matched_text}"
            )
            await self.notify_admin(msg)
            update_customer_status(c.id, "randevu_alindi",
                                   appt_ref=matched_text[:50])
            c2 = get_customer(c.id)
            if c2:
                c2.auto_book_enabled = 0
                c2.auto_book_status  = STATUS_BOOKED
                update_customer(c2)

        elif status_label in ("CLOSED", "ERROR", "CANCELLED"):
            logger.info(f"Auto-check sonuç: #{c.id} → {status_label}: {matched_text[:60]}")
            c2 = get_customer(c.id)
            if c2:
                c2.auto_book_status = STATUS_WAITING
                update_customer(c2)

        else:
            logger.info(f"Auto-check sonuç: #{c.id} → {status_label}: {matched_text[:60]}")
            c2 = get_customer(c.id)
            if c2:
                c2.auto_book_status = STATUS_WAITING
                update_customer(c2)

    # ──────────────────────────────────────────────────────────────────────────
    # Yardımcı
    # ──────────────────────────────────────────────────────────────────────────

    def _find_site(self, site_key: str) -> Optional[dict]:
        return next((s for s in self.sites if s.get("key") == site_key), None)
