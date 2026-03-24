#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Akıllı optimizasyon modülü.
Her girişim loglanır → proxy/hesap/saat bazında başarı istatistiği → sonraki denemede en iyi seçenek kullanılır.
"""

import sqlite3
import time
import logging
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "customers.db"
logger = logging.getLogger("visa-bot.optimizer")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_optimizer_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS attempt_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                proxy           TEXT    DEFAULT '',
                account         TEXT    DEFAULT '',
                result          TEXT    NOT NULL,
                cf_secs         REAL    DEFAULT 0,
                otp_secs        REAL    DEFAULT 0,
                total_secs      REAL    DEFAULT 0,
                hour_of_day     INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_log_ts      ON attempt_log (ts);
            CREATE INDEX IF NOT EXISTS idx_log_proxy   ON attempt_log (proxy);
            CREATE INDEX IF NOT EXISTS idx_log_account ON attempt_log (account);

            CREATE TABLE IF NOT EXISTS account_cooldown (
                account     TEXT PRIMARY KEY,
                until_ts    REAL NOT NULL,
                reason      TEXT DEFAULT ''
            );
        """)


def log_attempt(
    proxy: str,
    account: str,
    result: str,          # "success" | "rate_limited" | "cf_fail" | "otp_fail" | "error"
    cf_secs: float = 0,
    otp_secs: float = 0,
    total_secs: float = 0,
) -> None:
    now = time.time()
    hour = int(time.localtime(now).tm_hour)
    with _conn() as conn:
        conn.execute(
            """INSERT INTO attempt_log
               (ts, proxy, account, result, cf_secs, otp_secs, total_secs, hour_of_day)
               VALUES (?,?,?,?,?,?,?,?)""",
            (now, proxy, account, result, cf_secs, otp_secs, total_secs, hour),
        )
    logger.debug(f"Girişim loglandı: proxy={proxy!r} hesap={account!r} sonuç={result} cf={cf_secs:.1f}s")


def set_account_cooldown(account: str, seconds: int, reason: str = "") -> None:
    until = time.time() + seconds
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_cooldown (account, until_ts, reason) VALUES (?,?,?)",
            (account, until, reason),
        )
    logger.info(f"Hesap cooldown: {account!r} — {seconds}s — {reason}")


def is_account_on_cooldown(account: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT until_ts FROM account_cooldown WHERE account=?", (account,)
        ).fetchone()
    if not row:
        return False
    if time.time() < row["until_ts"]:
        remaining = int(row["until_ts"] - time.time())
        logger.info(f"Hesap soğumada: {account!r} — {remaining}s kaldı")
        return True
    return False


def get_cooldown_remaining(account: str) -> int:
    """Saniye cinsinden kalan cooldown süresi. 0 = hazır."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT until_ts FROM account_cooldown WHERE account=?", (account,)
        ).fetchone()
    if not row:
        return 0
    remaining = int(row["until_ts"] - time.time())
    return max(0, remaining)


def rank_proxies(proxies: list[dict], window_hours: int = 24) -> list[dict]:
    """
    Proxy listesini başarı oranına göre sıralar (en iyi önce).
    Hiç denemesi olmayan proxy'ler ortaya konur (bilinmiyor = şans ver).
    """
    if not proxies:
        return proxies

    since = time.time() - window_hours * 3600
    with _conn() as conn:
        rows = conn.execute(
            """SELECT proxy,
                      COUNT(*) as total,
                      SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) as wins
               FROM attempt_log
               WHERE ts > ?
               GROUP BY proxy""",
            (since,),
        ).fetchall()

    stats: dict[str, tuple[int, int]] = {r["proxy"]: (r["total"], r["wins"]) for r in rows}

    def _score(p: dict) -> float:
        server = p.get("server", "")
        total, wins = stats.get(server, (0, 0))
        if total == 0:
            return 0.5   # bilinmiyor — neutral
        return wins / total

    ranked = sorted(proxies, key=_score, reverse=True)
    if ranked:
        top_server = ranked[0].get("server", "?")
        top_score = _score(ranked[0])
        logger.debug(f"En iyi proxy: {top_server} — başarı: {top_score:.0%}")
    return ranked


def rank_accounts(accounts: list[dict], window_hours: int = 6) -> list[dict]:
    """
    Hesapları cooldown durumu + başarı oranına göre sıralar.
    Cooldown'daki hesaplar sona atılır.
    """
    if not accounts:
        return accounts

    since = time.time() - window_hours * 3600
    with _conn() as conn:
        rows = conn.execute(
            """SELECT account,
                      COUNT(*) as total,
                      SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) as wins
               FROM attempt_log
               WHERE ts > ?
               GROUP BY account""",
            (since,),
        ).fetchall()

    stats: dict[str, tuple[int, int]] = {r["account"]: (r["total"], r["wins"]) for r in rows}

    def _score(a: dict) -> float:
        em = a.get("email", "")
        if is_account_on_cooldown(em):
            return -1.0   # sona at
        total, wins = stats.get(em, (0, 0))
        if total == 0:
            return 0.5
        return wins / total

    return sorted(accounts, key=_score, reverse=True)


def best_cf_wait_secs(percentile: float = 0.75, window_hours: int = 48) -> float:
    """
    Geçmiş CF çözüm sürelerinden önerilen bekleme süresini döndürür.
    Varsayılan: son 48 saatin 75. persentili (aşırı hız azaltır başarısızlığı).
    """
    since = time.time() - window_hours * 3600
    with _conn() as conn:
        rows = conn.execute(
            """SELECT cf_secs FROM attempt_log
               WHERE ts > ? AND result='success' AND cf_secs > 0
               ORDER BY cf_secs""",
            (since,),
        ).fetchall()

    if not rows:
        return 8.0   # varsayılan

    values = [r["cf_secs"] for r in rows]
    idx = int(len(values) * percentile)
    idx = min(idx, len(values) - 1)
    suggested = values[idx]
    logger.debug(f"Önerilen CF bekleme: {suggested:.1f}s ({len(values)} örnek)")
    return max(5.0, min(suggested, 20.0))  # 5-20s arası sınırla


def hourly_success_report(window_days: int = 7) -> dict[int, float]:
    """Saate göre başarı oranı döndürür (0-23)."""
    since = time.time() - window_days * 86400
    with _conn() as conn:
        rows = conn.execute(
            """SELECT hour_of_day,
                      COUNT(*) as total,
                      SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) as wins
               FROM attempt_log
               WHERE ts > ?
               GROUP BY hour_of_day""",
            (since,),
        ).fetchall()

    return {
        r["hour_of_day"]: (r["wins"] / r["total"] if r["total"] > 0 else 0.0)
        for r in rows
    }


def best_hours(top_n: int = 3, window_days: int = 7) -> list[int]:
    """En yüksek başarı oranlı N saati döndürür."""
    report = hourly_success_report(window_days)
    if not report:
        return []
    return sorted(report, key=lambda h: report[h], reverse=True)[:top_n]


def stats_summary(window_hours: int = 24) -> str:
    """Son N saatin istatistik özeti (Telegram mesajı için)."""
    since = time.time() - window_hours * 3600
    with _conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN result='rate_limited' THEN 1 ELSE 0 END) as rl,
                      SUM(CASE WHEN result='cf_fail' THEN 1 ELSE 0 END) as cf,
                      AVG(CASE WHEN result='success' THEN cf_secs END) as avg_cf,
                      AVG(CASE WHEN result='success' THEN total_secs END) as avg_total
               FROM attempt_log WHERE ts > ?""",
            (since,),
        ).fetchone()

    if not row or not row["total"]:
        return f"Son {window_hours} saatte girişim yok."

    total = row["total"]
    wins = row["wins"] or 0
    rate = wins / total if total else 0
    avg_cf = f"{row['avg_cf']:.1f}s" if row["avg_cf"] else "—"
    avg_total = f"{row['avg_total']:.0f}s" if row["avg_total"] else "—"

    top = best_hours()
    top_str = ", ".join(f"{h:02d}:00" for h in top) if top else "—"

    return (
        f"📊 *Son {window_hours} saat istatistikleri:*\n"
        f"✅ Başarı: {wins}/{total} ({rate:.0%})\n"
        f"🚫 Rate-limit: {row['rl'] or 0}\n"
        f"🛡️ CF başarısız: {row['cf'] or 0}\n"
        f"⏱️ Ort. CF süresi: {avg_cf}\n"
        f"⏱️ Ort. toplam süre: {avg_total}\n"
        f"🕐 En iyi saatler: {top_str}"
    )


# Başlatma
init_optimizer_db()
