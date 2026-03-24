#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Müşteri veritabanı modülü.
SQLite tabanlı, thread-safe CRUD işlemleri.
"""

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "customers.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL,
                passport_no       TEXT,
                nationality       TEXT,
                phone             TEXT,
                email             TEXT,
                visa_type         TEXT,
                destination       TEXT,
                travel_date       TEXT,
                status            TEXT    DEFAULT 'bekliyor',
                appt_date         TEXT,
                appt_ref          TEXT,
                center            TEXT,
                notes             TEXT,
                vfs_account           TEXT    DEFAULT '',
                vfs_email             TEXT    DEFAULT '',
                vfs_password          TEXT    DEFAULT '',
                vfs_imap_password     TEXT    DEFAULT '',
                auto_book_enabled     INTEGER DEFAULT 0,
                auto_book_mode        TEXT    DEFAULT '',
                auto_book_site_key    TEXT    DEFAULT '',
                auto_book_last_check  REAL    DEFAULT 0,
                auto_book_status      TEXT    DEFAULT '',
                created_at            REAL    DEFAULT (unixepoch())
            );

            CREATE INDEX IF NOT EXISTS idx_customers_name
                ON customers (name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_customers_passport
                ON customers (passport_no);
            CREATE INDEX IF NOT EXISTS idx_customers_status
                ON customers (status);
        """)
        # Migration: mevcut DB için eksik kolonları ekle
        existing = {row[1] for row in conn.execute("PRAGMA table_info(customers)")}
        for col, typedef in [
            ("vfs_account",          "TEXT DEFAULT ''"),
            ("vfs_email",            "TEXT DEFAULT ''"),
            ("vfs_password",         "TEXT DEFAULT ''"),
            ("vfs_imap_password",    "TEXT DEFAULT ''"),
            ("auto_book_enabled",    "INTEGER DEFAULT 0"),
            ("auto_book_mode",       "TEXT DEFAULT ''"),
            ("auto_book_site_key",   "TEXT DEFAULT ''"),
            ("auto_book_last_check", "REAL DEFAULT 0"),
            ("auto_book_status",     "TEXT DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE customers ADD COLUMN {col} {typedef}")


@dataclass
class Customer:
    id: Optional[int] = None
    name: str = ""
    passport_no: str = ""
    nationality: str = ""
    phone: str = ""
    email: str = ""
    visa_type: str = ""
    destination: str = ""
    travel_date: str = ""
    status: str = "bekliyor"
    appt_date: str = ""
    appt_ref: str = ""
    center: str = ""
    notes: str = ""
    vfs_account: str = ""
    vfs_email: str = ""
    vfs_password: str = ""
    vfs_imap_password: str = ""
    auto_book_enabled: int = 0
    auto_book_mode: str = ""          # "notify" | "auto"
    auto_book_site_key: str = ""
    auto_book_last_check: float = 0.0
    auto_book_status: str = ""
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Customer":
        return cls(**{k: (row[k] or "") for k in row.keys()})

    def status_emoji(self) -> str:
        return {
            "bekliyor":   "⏳",
            "randevu_alindi": "✅",
            "tamamlandi": "🎉",
            "iptal":      "❌",
        }.get(self.status, "🟡")

    def to_text(self, short: bool = False) -> str:
        emoji = self.status_emoji()
        if short:
            dest = f" → {self.destination}" if self.destination else ""
            date = f" | {self.travel_date}" if self.travel_date else ""
            return f"{emoji} *{self.name}*{dest}{date}"

        lines = [
            f"👤 *{self.name}*",
            f"🛂 Pasaport: `{self.passport_no}`" if self.passport_no else "",
            f"🌍 Uyruk: {self.nationality}"       if self.nationality else "",
            f"📋 Vize Tipi: {self.visa_type}"     if self.visa_type else "",
            f"✈️ Hedef: {self.destination}"        if self.destination else "",
            f"🗓️ Seyahat: {self.travel_date}"     if self.travel_date else "",
            f"📞 Tel: {self.phone}"                if self.phone else "",
            f"📧 E-posta: {self.email}"            if self.email else "",
            f"🚦 Durum: {emoji} {self.status}",
            f"📅 Randevu: {self.appt_date}"        if self.appt_date else "📅 Randevu: —",
            f"🔖 Ref: `{self.appt_ref}`"           if self.appt_ref else "",
            f"🏢 Merkez: {self.center}"            if self.center else "",
            f"📝 Not: {self.notes}"                if self.notes else "",
            f"🔑 VFS: `{self.vfs_email}`" if self.vfs_email else "🔑 VFS: kayıtlı değil",
            (f"🤖 {'Otomatik Randevu' if self.auto_book_mode == 'auto' else 'Otomatik Takip'}: "
             f"✅ aktif ({self.auto_book_site_key})"
             if self.auto_book_enabled else ""),
        ]
        return "\n".join(l for l in lines if l)


# ---------- CRUD ----------

def add_customer(c: Customer) -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO customers
               (name, passport_no, nationality, phone, email,
                visa_type, destination, travel_date, status,
                appt_date, appt_ref, center, notes, vfs_account,
                vfs_email, vfs_password, vfs_imap_password,
                auto_book_enabled, auto_book_mode, auto_book_site_key,
                auto_book_last_check, auto_book_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.name, c.passport_no, c.nationality, c.phone, c.email,
             c.visa_type, c.destination, c.travel_date, c.status,
             c.appt_date, c.appt_ref, c.center, c.notes, c.vfs_account,
             c.vfs_email, c.vfs_password, c.vfs_imap_password,
             c.auto_book_enabled, c.auto_book_mode, c.auto_book_site_key,
             c.auto_book_last_check, c.auto_book_status),
        )
        return cur.lastrowid


def get_customer(customer_id: int) -> Optional[Customer]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE id=?", (customer_id,)
        ).fetchone()
    return Customer.from_row(row) if row else None


def update_customer(c: Customer) -> None:
    with _get_conn() as conn:
        conn.execute(
            """UPDATE customers SET
               name=?, passport_no=?, nationality=?, phone=?, email=?,
               visa_type=?, destination=?, travel_date=?, status=?,
               appt_date=?, appt_ref=?, center=?, notes=?, vfs_account=?,
               vfs_email=?, vfs_password=?, vfs_imap_password=?,
               auto_book_enabled=?, auto_book_mode=?, auto_book_site_key=?,
               auto_book_last_check=?, auto_book_status=?
               WHERE id=?""",
            (c.name, c.passport_no, c.nationality, c.phone, c.email,
             c.visa_type, c.destination, c.travel_date, c.status,
             c.appt_date, c.appt_ref, c.center, c.notes, c.vfs_account,
             c.vfs_email, c.vfs_password, c.vfs_imap_password,
             c.auto_book_enabled, c.auto_book_mode, c.auto_book_site_key,
             c.auto_book_last_check, c.auto_book_status, c.id),
        )


def update_customer_status(customer_id: int, status: str,
                           appt_date: str = "", appt_ref: str = "",
                           center: str = "") -> None:
    with _get_conn() as conn:
        conn.execute(
            """UPDATE customers SET status=?,
               appt_date=CASE WHEN ?!='' THEN ? ELSE appt_date END,
               appt_ref=CASE WHEN ?!='' THEN ? ELSE appt_ref END,
               center=CASE WHEN ?!='' THEN ? ELSE center END
               WHERE id=?""",
            (status,
             appt_date, appt_date,
             appt_ref, appt_ref,
             center, center,
             customer_id),
        )


def delete_customer(customer_id: int) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))


def search_customers(query: str) -> list[Customer]:
    q = f"%{query}%"
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM customers
               WHERE name LIKE ? OR passport_no LIKE ?
               ORDER BY created_at DESC LIMIT 20""",
            (q, q),
        ).fetchall()
    return [Customer.from_row(r) for r in rows]


def list_customers(status: Optional[str] = None, limit: int = 30, offset: int = 0) -> list[Customer]:
    with _get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM customers WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM customers ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [Customer.from_row(r) for r in rows]


def customer_count() -> dict[str, int]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM customers GROUP BY status"
        ).fetchall()
    return {r["status"]: r["cnt"] for r in rows}


# Başlatma
init_db()
