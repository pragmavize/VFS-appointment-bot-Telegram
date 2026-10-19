#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VISABot Dashboard — Real-time web arayüzü
Port: 8585
"""

import os
import re
import time
import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, render_template_string, Response, jsonify, session, request, redirect, url_for
from flask_socketio import SocketIO, emit

BASE_DIR       = Path(__file__).resolve().parent
DB_PATH        = BASE_DIR / "data" / "customers.db"
LOG_PATH       = BASE_DIR / "bot.log"
PROXIES_FILE   = BASE_DIR / "proxies.txt"
ROTATION_FILE  = BASE_DIR / "data" / "proxy_rotation.json"
BOT_SCRIPT     = "visa_watch_onefile.py"

app = Flask(__name__)
app.config["SECRET_KEY"] = "visabot_dash_2026_xK9mP"
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7 gün
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "kaven667")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "botan12K!")

_PUBLIC_PATHS = {"/login", "/logout", "/healthz"}

# Son 500 log satırı bellekte tut
_log_buffer: deque = deque(maxlen=500)
_log_lock = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# Yardımcılar
# ──────────────────────────────────────────────────────────────────────────────

def _db():
    if not DB_PATH.exists():
        return None
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _bot_pid() -> int | None:
    """Çalışan bot PID'ini döndürür."""
    try:
        import subprocess
        out = subprocess.check_output(
            ["pgrep", "-f", BOT_SCRIPT], text=True
        ).strip()
        pids = [int(p) for p in out.split() if p.isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _bot_uptime(pid: int | None) -> str:
    if not pid:
        return "-"
    try:
        import subprocess
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "etime="], text=True
        ).strip()
        return out
    except Exception:
        return "-"


def _stats(hours: int = 24) -> dict:
    conn = _db()
    if not conn:
        return {}
    since = time.time() - hours * 3600
    try:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN result='success'      THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN result='rate_limited' THEN 1 ELSE 0 END) as rl,
                   SUM(CASE WHEN result='cf_fail'      THEN 1 ELSE 0 END) as cf,
                   SUM(CASE WHEN result='error'        THEN 1 ELSE 0 END) as err,
                   AVG(CASE WHEN result='success' THEN cf_secs  END) as avg_cf,
                   AVG(CASE WHEN result='success' THEN total_secs END) as avg_total
            FROM attempt_log WHERE ts > ?
        """, (since,)).fetchone()
        total = row["total"] or 0
        wins  = row["wins"]  or 0
        return {
            "total": total,
            "wins":  wins,
            "rate":  round(wins / total * 100, 1) if total else 0,
            "rate_limited": row["rl"]  or 0,
            "cf_fail":      row["cf"]  or 0,
            "errors":       row["err"] or 0,
            "avg_cf":    round(row["avg_cf"]    or 0, 1),
            "avg_total": round(row["avg_total"] or 0, 0),
        }
    except Exception:
        return {}
    finally:
        conn.close()


def _hourly_chart(days: int = 7) -> list:
    conn = _db()
    if not conn:
        return []
    since = time.time() - days * 86400
    try:
        rows = conn.execute("""
            SELECT hour_of_day,
                   COUNT(*) as total,
                   SUM(CASE WHEN result='success' THEN 1 ELSE 0 END) as wins
            FROM attempt_log WHERE ts > ?
            GROUP BY hour_of_day ORDER BY hour_of_day
        """, (since,)).fetchall()
        return [{"hour": r["hour_of_day"],
                 "rate": round(r["wins"] / r["total"] * 100, 1) if r["total"] else 0,
                 "total": r["total"]} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _recent_attempts(limit: int = 20) -> list:
    conn = _db()
    if not conn:
        return []
    try:
        rows = conn.execute("""
            SELECT ts, proxy, account, result, cf_secs, total_secs
            FROM attempt_log
            ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "time":    datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S"),
                "proxy":   (r["proxy"] or "direct")[:30],
                "account": (r["account"] or "-")[:30],
                "result":  r["result"],
                "cf":      round(r["cf_secs"] or 0, 1),
                "total":   round(r["total_secs"] or 0, 0),
            })
        return out
    except Exception:
        return []
    finally:
        conn.close()


def _customers_summary() -> dict:
    conn = _db()
    if not conn:
        return {}
    try:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM customers GROUP BY status
        """).fetchall()
        return {r["status"]: r["cnt"] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def _recent_errors(limit: int = 30) -> list:
    """Bot log'undan son ERROR satırlarını döndürür."""
    errors = []
    try:
        lines = LOG_PATH.read_text(errors="replace").splitlines()
        for line in reversed(lines):
            if "] ERROR:" in line or "] WARNING:" in line:
                errors.append(line.strip())
                if len(errors) >= limit:
                    break
    except Exception:
        pass
    return errors


# ──────────────────────────────────────────────────────────────────────────────
# Proxy yönetimi
# ──────────────────────────────────────────────────────────────────────────────

def _load_rotation_urls() -> dict:
    """proxy_rotation.json yükle: {server: {url, label}}"""
    try:
        if ROTATION_FILE.exists():
            return json.loads(ROTATION_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_rotation_urls(data: dict) -> None:
    ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROTATION_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _read_proxies() -> list[dict]:
    """proxies.txt oku → [{server, host, port, user, pass, proto, active, rotation_url, label}]
    Format: [#]ip:port[:user:pass[:proto]]  — # ile başlayanlar devre dışı
    """
    rotations = _load_rotation_urls()
    proxies = []
    try:
        lines = PROXIES_FILE.read_text().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            active = not line.startswith("#")
            clean  = line.lstrip("#").strip()
            parts  = clean.split(":")
            host  = parts[0]
            port  = parts[1] if len(parts) > 1 else ""
            user  = parts[2] if len(parts) > 2 else ""
            pwd   = parts[3] if len(parts) > 3 else ""
            proto = parts[4].strip().lower() if len(parts) > 4 else "http"
            if proto not in ("http", "socks5"):
                proto = "http"
            server = f"{host}:{port}"
            rot = rotations.get(server, {})
            proxies.append({
                "server":       server,
                "host":         host,
                "port":         port,
                "user":         user,
                "password":     pwd,
                "proto":        proto,
                "active":       active,
                "rotation_url": rot.get("url", ""),
                "label":        rot.get("label", server),
            })
    except Exception:
        pass
    return proxies


def _write_proxies(proxies: list[dict]) -> None:
    lines = []
    for p in proxies:
        user   = p.get("user", "")
        pwd    = p.get("password", "")
        proto  = p.get("proto", "http")
        active = p.get("active", True)
        if user and pwd:
            line = f"{p['host']}:{p['port']}:{user}:{pwd}:{proto}"
        else:
            line = f"{p['host']}:{p['port']}"
        if not active:
            line = "#" + line
        lines.append(line)
    PROXIES_FILE.write_text("\n".join(lines) + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Log tail thread — yeni satırları socket üzerinden iter
# ──────────────────────────────────────────────────────────────────────────────

def _tail_log():
    """Sürekli log dosyasını izle, yeni satırları broadcast et."""
    while True:
        try:
            with open(LOG_PATH, "r", errors="replace") as f:
                f.seek(0, 2)  # EOF
                while True:
                    line = f.readline()
                    if line:
                        line = line.rstrip()
                        with _log_lock:
                            _log_buffer.append(line)
                        # Seviyeye göre renk etiketi
                        level = "info"
                        if "] ERROR:" in line:
                            level = "error"
                        elif "] WARNING:" in line:
                            level = "warning"
                        elif "başarılı" in line.lower() or "geçildi" in line.lower() or "tıklandı" in line.lower():
                            level = "success"
                        socketio.emit("log_line", {"line": line, "level": level})
                    else:
                        time.sleep(0.3)
        except Exception:
            time.sleep(2)


# ──────────────────────────────────────────────────────────────────────────────
# HTML Template
# ──────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VISABot Dashboard</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:      #0d1117;
    --bg2:     #161b22;
    --bg3:     #21262d;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #3fb950;
    --yellow:  #d29922;
    --red:     #f85149;
    --blue:    #58a6ff;
    --purple:  #bc8cff;
    --orange:  #ffa657;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  /* ── Top bar ── */
  .topbar {
    background: var(--bg2);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .logo { font-size: 20px; font-weight: 700; color: var(--blue); letter-spacing: -0.5px; }
  .logo span { color: var(--green); }
  .bot-badge {
    display: flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 20px;
    font-size: 12px; font-weight: 600;
    background: var(--bg3); border: 1px solid var(--border);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; }
  .dot.online  { background: var(--green);  box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
  .dot.offline { background: var(--red); }
  @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.4 } }
  .topbar-right { display: flex; gap: 8px; align-items: center; color: var(--muted); font-size: 12px; }
  #clock { font-variant-numeric: tabular-nums; }

  /* ── Layout ── */
  .main { padding: 20px 24px; max-width: 1600px; margin: 0 auto; }
  .grid-4 { display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; margin-bottom: 20px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .grid-3 { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media(max-width:1100px){ .grid-4{grid-template-columns:repeat(2,1fr)} .grid-2,.grid-3{grid-template-columns:1fr} }
  @media(max-width:600px){ .grid-4{grid-template-columns:1fr} }

  /* ── Card ── */
  .card {
    background: var(--bg2); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; position: relative; overflow: hidden;
  }
  .card-title { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-bottom: 12px; }
  .card-value { font-size: 32px; font-weight: 700; line-height: 1; }
  .card-sub   { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .card-accent { position: absolute; top:0; right:0; width:80px; height:80px; border-radius: 0 12px 0 80px; opacity:.15; }
  .accent-green  { background: var(--green); }
  .accent-red    { background: var(--red);   }
  .accent-yellow { background: var(--yellow); }
  .accent-blue   { background: var(--blue);  }
  .accent-purple { background: var(--purple); }

  /* ── Log terminal ── */
  .log-box {
    background: #010409; border: 1px solid var(--border); border-radius: 12px;
    height: 380px; overflow-y: auto; padding: 12px 16px;
    font-family: 'Cascadia Code','Fira Code',monospace; font-size: 11.5px; line-height: 1.6;
  }
  .log-box::-webkit-scrollbar { width: 6px; }
  .log-box::-webkit-scrollbar-track { background: transparent; }
  .log-box::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
  .log-line.error   { color: var(--red);    background: rgba(248,81,73,.06);  }
  .log-line.warning { color: var(--yellow); }
  .log-line.success { color: var(--green);  }
  .log-line.info    { color: var(--muted);  }
  .log-controls { display:flex; gap:8px; margin-bottom:8px; align-items:center; }
  .log-controls input { background:var(--bg3); border:1px solid var(--border); border-radius:6px; color:var(--text); padding:5px 10px; font-size:12px; flex:1; outline:none; }

  /* ── Table ── */
  .tbl { width:100%; border-collapse:collapse; font-size:12px; }
  .tbl th { color:var(--muted); font-weight:600; text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); text-transform:uppercase; letter-spacing:.5px; font-size:10px; }
  .tbl td { padding:8px 10px; border-bottom:1px solid rgba(48,54,61,.5); vertical-align:middle; }
  .tbl tr:last-child td { border-bottom:none; }
  .tbl tr:hover td { background: rgba(88,166,255,.04); }
  .badge { display:inline-block; padding:2px 8px; border-radius:20px; font-size:10px; font-weight:700; }
  .badge.success { background:rgba(63,185,80,.15); color:var(--green); }
  .badge.error   { background:rgba(248,81,73,.15);  color:var(--red);   }
  .badge.warning,.badge.rate_limited { background:rgba(210,153,34,.15); color:var(--yellow); }
  .badge.cf_fail  { background:rgba(188,140,255,.15); color:var(--purple); }
  .badge.unknown  { background:rgba(139,148,158,.15); color:var(--muted); }

  /* ── Customers pill ── */
  .pill-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
  .pill { padding:5px 14px; border-radius:20px; font-size:12px; font-weight:600; border:1px solid; }
  .pill.active  { background:rgba(63,185,80,.12);  color:var(--green);  border-color:rgba(63,185,80,.3); }
  .pill.pending { background:rgba(88,166,255,.12); color:var(--blue);   border-color:rgba(88,166,255,.3); }
  .pill.booked  { background:rgba(210,153,34,.12); color:var(--yellow); border-color:rgba(210,153,34,.3); }
  .pill.other   { background:rgba(139,148,158,.1); color:var(--muted);  border-color:var(--border); }

  /* ── Error list ── */
  .err-list { max-height:200px; overflow-y:auto; }
  .err-item { padding:6px 0; border-bottom:1px solid rgba(48,54,61,.4); font-size:11px; color:var(--red); word-break:break-all; }
  .err-item.warn { color:var(--yellow); }
  .err-item:last-child { border-bottom:none; }

  /* ── Proxy status badge ── */
  .prx-badge { display:inline-block; padding:3px 8px; border-radius:6px; font-size:11px; font-weight:700; }
  .prx-badge.online   { background:rgba(63,185,80,.15);  color:var(--green);  border:1px solid rgba(63,185,80,.3); }
  .prx-badge.offline  { background:rgba(248,81,73,.15);  color:var(--red);    border:1px solid rgba(248,81,73,.3); }
  .prx-badge.checking { background:rgba(88,166,255,.12); color:var(--blue);   border:1px solid rgba(88,166,255,.3); }
  .prx-badge.unknown  { background:rgba(139,148,158,.1); color:var(--muted);  border:1px solid var(--border); }

  /* ── Proxy form inputs ── */
  .px-inp { background:var(--bg2); border:1px solid var(--border); border-radius:6px; color:var(--text); padding:6px 10px; font-size:12px; outline:none; }
  .px-inp:focus { border-color:var(--blue); }
  .rot-url { max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--blue); font-size:11px; }
  .rot-btn { padding:4px 10px; font-size:11px; border-radius:6px; border:1px solid rgba(63,185,80,.3); background:rgba(63,185,80,.1); color:var(--green); cursor:pointer; font-weight:600; }
  .rot-btn:hover { filter:brightness(1.3); }
  .rot-btn.loading { opacity:.5; cursor:wait; }
  .rot-status { font-size:10px; color:var(--muted); margin-top:2px; }

  /* ── Buttons ── */
  .btn { padding:7px 16px; border-radius:8px; border:none; cursor:pointer; font-size:12px; font-weight:600; transition:.15s; }
  .btn-green  { background:rgba(63,185,80,.15);  color:var(--green);  border:1px solid rgba(63,185,80,.3); }
  .btn-red    { background:rgba(248,81,73,.15);  color:var(--red);    border:1px solid rgba(248,81,73,.3); }
  .btn-blue   { background:rgba(88,166,255,.15); color:var(--blue);   border:1px solid rgba(88,166,255,.3); }
  .btn-yellow { background:rgba(210,153,34,.15); color:var(--yellow); border:1px solid rgba(210,153,34,.3); }
  .btn:hover  { filter:brightness(1.2); }
  .btn-row    { display:flex; gap:8px; flex-wrap:wrap; }
  .section-title { font-size:13px; font-weight:700; color:var(--text); margin-bottom:12px; display:flex; align-items:center; gap:8px; }
  .section-title span { color:var(--blue); font-size:16px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <div class="logo">VISA<span>Bot</span> Dashboard</div>
    <div class="bot-badge">
      <div class="dot" id="statusDot"></div>
      <span id="statusText">Kontrol ediliyor...</span>
      <span style="color:var(--muted);margin-left:4px" id="uptime"></span>
    </div>
  </div>
  <div class="topbar-right">
    <span>🕐</span><span id="clock"></span>
    <span style="margin-left:12px">⟳ <span id="lastUpdate">-</span></span>
    <button class="btn btn-blue" onclick="refreshAll()" style="margin-left:8px">Yenile</button>
  </div>
</div>

<div class="main">

  <!-- Stat cards -->
  <div class="grid-4" id="statCards">
    <div class="card">
      <div class="card-accent accent-green"></div>
      <div class="card-title">✅ Başarılı (24s)</div>
      <div class="card-value" id="stat-wins">—</div>
      <div class="card-sub" id="stat-rate">başarı oranı: —</div>
    </div>
    <div class="card">
      <div class="card-accent accent-blue"></div>
      <div class="card-title">📊 Toplam Deneme</div>
      <div class="card-value" id="stat-total">—</div>
      <div class="card-sub" id="stat-avgtotal">ort. süre: —</div>
    </div>
    <div class="card">
      <div class="card-accent accent-yellow"></div>
      <div class="card-title">🚫 Rate-Limit</div>
      <div class="card-value" id="stat-rl">—</div>
      <div class="card-sub" id="stat-avgcf">ort. CF: —</div>
    </div>
    <div class="card">
      <div class="card-accent accent-red"></div>
      <div class="card-title">❌ Hata</div>
      <div class="card-value" id="stat-err">—</div>
      <div class="card-sub" id="stat-cf">CF başarısız: —</div>
    </div>
  </div>

  <!-- Log + Chart -->
  <div class="grid-3">
    <div class="card">
      <div class="section-title"><span>📟</span> Canlı Log</div>
      <div class="log-controls">
        <input type="text" id="logFilter" placeholder="Filtrele... (örn: ERROR, Turnstile, OTP)" oninput="filterLog()">
        <button class="btn btn-blue" onclick="clearLog()">Temizle</button>
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--muted);cursor:pointer">
          <input type="checkbox" id="autoScroll" checked> Otomatik kaydır
        </label>
      </div>
      <div class="log-box" id="logBox"></div>
    </div>
    <div>
      <div class="card" style="margin-bottom:16px">
        <div class="section-title"><span>📈</span> Saatlik Başarı Oranı</div>
        <canvas id="hourlyChart" height="130"></canvas>
      </div>
      <div class="card">
        <div class="section-title"><span>👥</span> Müşteriler</div>
        <div class="pill-row" id="customerPills">—</div>
      </div>
    </div>
  </div>

  <!-- Proxy Yönetimi -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title" style="justify-content:space-between">
      <span style="display:flex;align-items:center;gap:8px"><span>🌐</span> Proxy Yönetimi</span>
      <button class="btn btn-blue" onclick="document.getElementById('addProxyForm').style.display=document.getElementById('addProxyForm').style.display==='none'?'flex':'none'">+ Proxy Ekle</button>
    </div>

    <!-- Yeni proxy formu -->
    <div id="addProxyForm" style="display:none;gap:8px;flex-wrap:wrap;margin-bottom:16px;padding:14px;background:var(--bg3);border-radius:8px;border:1px solid var(--border)">
      <select id="px-proto" class="px-inp" style="width:110px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px">
        <option value="http">HTTP</option>
        <option value="socks5">SOCKS5</option>
      </select>
      <input id="px-host"  class="px-inp" placeholder="IP / Host"   style="width:160px">
      <input id="px-port"  class="px-inp" placeholder="Port"        style="width:80px">
      <input id="px-user"  class="px-inp" placeholder="Kullanıcı"   style="width:130px">
      <input id="px-pass"  class="px-inp" placeholder="Şifre"       style="width:130px">
      <input id="px-label" class="px-inp" placeholder="Etiket (opsiyonel)" style="width:160px">
      <input id="px-rot"   class="px-inp" placeholder="IP Rotasyon URL (opsiyonel)" style="width:280px">
      <button class="btn btn-green" onclick="addProxy()">Ekle</button>
    </div>

    <!-- Proxy tablosu -->
    <div style="overflow-x:auto">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <button class="btn btn-blue" onclick="checkAllProxies()">🔍 Tümünü Test Et</button>
      <span id="checkAllStatus" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <table class="tbl" id="proxyTable">
      <thead><tr>
        <th>Durum</th><th>Etiket / Server</th><th>Host</th><th>Port</th><th>Kullanıcı</th><th>Rotasyon URL</th><th>İşlemler</th>
      </tr></thead>
      <tbody id="proxyBody"></tbody>
    </table>
    </div>
  </div>

  <!-- Attempts table + Errors -->
  <div class="grid-2">
    <div class="card">
      <div class="section-title"><span>🔄</span> Son Denemeler</div>
      <table class="tbl">
        <thead><tr>
          <th>Saat</th><th>Hesap</th><th>Proxy</th><th>Sonuç</th><th>CF</th><th>Toplam</th>
        </tr></thead>
        <tbody id="attemptsBody"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="section-title" style="justify-content:space-between">
        <span style="display:flex;align-items:center;gap:8px"><span>🔴</span> Bot Hata Mesajları</span>
        <div class="btn-row">
          <button class="btn btn-green"  onclick="botAction('start')">▶ Başlat</button>
          <button class="btn btn-red"    onclick="botAction('stop')">⏹ Durdur</button>
          <button class="btn btn-yellow" onclick="botAction('restart')">↺ Yeniden Başlat</button>
        </div>
      </div>
      <div class="err-list" id="errList"></div>
    </div>
  </div>

</div>

<script>
const socket = io();
let chart = null;
let allLogLines = [];

// ── Clock ──
function updateClock(){
  const n = new Date();
  document.getElementById('clock').textContent =
    n.toLocaleTimeString('tr-TR',{hour12:false});
}
setInterval(updateClock, 1000); updateClock();

// ── Socket log ──
socket.on('log_line', d => {
  allLogLines.push(d);
  if(allLogLines.length > 2000) allLogLines.shift();
  const filter = document.getElementById('logFilter').value.toLowerCase();
  if(!filter || d.line.toLowerCase().includes(filter)) appendLogLine(d);
});

function appendLogLine(d){
  const box = document.getElementById('logBox');
  const el = document.createElement('div');
  el.className = 'log-line ' + (d.level || 'info');
  el.textContent = d.line;
  box.appendChild(el);
  if(document.getElementById('autoScroll').checked)
    box.scrollTop = box.scrollHeight;
  // Max 300 satır DOM'da tut
  while(box.children.length > 300) box.removeChild(box.firstChild);
}

function filterLog(){
  const filter = document.getElementById('logFilter').value.toLowerCase();
  const box = document.getElementById('logBox');
  box.innerHTML = '';
  allLogLines.filter(d => !filter || d.line.toLowerCase().includes(filter))
    .slice(-300).forEach(appendLogLine);
}

function clearLog(){ document.getElementById('logBox').innerHTML=''; allLogLines=[]; }

// ── API refresh ──
async function refreshAll(){
  try {
    const [stats, chart_, attempts, errors, status, cust] = await Promise.all([
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/chart').then(r=>r.json()),
      fetch('/api/attempts').then(r=>r.json()),
      fetch('/api/errors').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/customers').then(r=>r.json()),
    ]);
    renderStats(stats);
    renderChart(chart_);
    renderAttempts(attempts);
    renderErrors(errors);
    renderStatus(status);
    renderCustomers(cust);
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('tr-TR',{hour12:false});
  } catch(e){ console.error(e); }
}

function renderStats(s){
  if(!s || !Object.keys(s).length) return;
  document.getElementById('stat-wins').textContent  = s.wins ?? '—';
  document.getElementById('stat-rate').textContent  = 'başarı oranı: ' + (s.rate ?? '—') + '%';
  document.getElementById('stat-total').textContent = s.total ?? '—';
  document.getElementById('stat-avgtotal').textContent = 'ort. süre: ' + (s.avg_total ?? '—') + 's';
  document.getElementById('stat-rl').textContent    = s.rate_limited ?? '—';
  document.getElementById('stat-avgcf').textContent = 'ort. CF: ' + (s.avg_cf ?? '—') + 's';
  document.getElementById('stat-err').textContent   = s.errors ?? '—';
  document.getElementById('stat-cf').textContent    = 'CF başarısız: ' + (s.cf_fail ?? '—');
}

function renderChart(data){
  const labels = data.map(d => d.hour+':00');
  const rates  = data.map(d => d.rate);
  const ctx = document.getElementById('hourlyChart').getContext('2d');
  if(chart){ chart.destroy(); }
  chart = new Chart(ctx, {
    type:'bar',
    data:{
      labels,
      datasets:[{
        label:'Başarı %',
        data: rates,
        backgroundColor: rates.map(r => r>60?'rgba(63,185,80,.6)':r>30?'rgba(210,153,34,.6)':'rgba(248,81,73,.6)'),
        borderRadius:4,
      }]
    },
    options:{
      responsive:true, plugins:{legend:{display:false}},
      scales:{
        y:{min:0,max:100,ticks:{color:'#8b949e',font:{size:10}},grid:{color:'#21262d'}},
        x:{ticks:{color:'#8b949e',font:{size:10}},grid:{display:false}}
      }
    }
  });
}

function renderAttempts(rows){
  const badge = r => `<span class="badge ${r}">${r}</span>`;
  document.getElementById('attemptsBody').innerHTML = rows.map(r=>
    `<tr>
      <td>${r.time}</td>
      <td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">${r.account}</td>
      <td style="color:var(--muted)">${r.proxy}</td>
      <td>${badge(r.result)}</td>
      <td style="color:var(--muted)">${r.cf}s</td>
      <td style="color:var(--muted)">${r.total}s</td>
    </tr>`
  ).join('');
}

function renderErrors(errs){
  document.getElementById('errList').innerHTML = errs.map(e=>
    `<div class="err-item ${e.includes('WARNING')?'warn':''}">${escHtml(e)}</div>`
  ).join('') || '<div style="color:var(--muted);font-size:12px;padding:8px 0">Hata yok ✓</div>';
}

function renderStatus(s){
  const dot  = document.getElementById('statusDot');
  const txt  = document.getElementById('statusText');
  const upt  = document.getElementById('uptime');
  if(s.running){
    dot.className = 'dot online';
    txt.textContent = 'Çalışıyor (PID:' + s.pid + ')';
    upt.textContent = '⏱ ' + s.uptime;
  } else {
    dot.className = 'dot offline';
    txt.textContent = 'Durdurulmuş';
    upt.textContent = '';
  }
}

function renderCustomers(cust){
  const colors = {ACTIVE:'active',PENDING:'pending',BOOKED:'booked'};
  const pills = Object.entries(cust).map(([k,v])=>
    `<div class="pill ${colors[k]||'other'}">${k}: ${v}</div>`
  ).join('');
  document.getElementById('customerPills').innerHTML = pills || '<span style="color:var(--muted)">Kayıt yok</span>';
}

function escHtml(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── Proxy yönetimi ──
let _proxyList = [];

async function loadProxies(){
  const r = await fetch('/api/proxies');
  _proxyList = await r.json();
  renderProxyTable(_proxyList, {});
}

function renderProxyTable(proxies, statusMap){
  const tbody = document.getElementById('proxyBody');
  if(!proxies.length){
    tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);text-align:center;padding:16px">Proxy yok. Eklemek için + butonu kullan.</td></tr>';
    return;
  }
  tbody.innerHTML = proxies.map((p,i) => {
    const st = statusMap[p.server];
    let statusCell;
    if(!st){
      statusCell = `<span class="prx-badge unknown" id="pst-${i}">— bilinmiyor</span>
        <br><button class="rot-btn" style="margin-top:3px" onclick="checkProxy('${escHtml(p.server)}',${i})">🔍 Test</button>`;
    } else if(st.checking){
      statusCell = `<span class="prx-badge checking" id="pst-${i}">⏳ Test ediliyor...</span>`;
    } else if(st.ok){
      statusCell = `<span class="prx-badge online" id="pst-${i}">✅ ${st.ms}ms</span>
        <br><button class="rot-btn" style="margin-top:3px" onclick="checkProxy('${escHtml(p.server)}',${i})">↺ Yeniden test</button>`;
    } else {
      statusCell = `<span class="prx-badge offline" id="pst-${i}">❌ ${escHtml(st.msg||'Hata')}</span>
        <br><button class="rot-btn" style="margin-top:3px;border-color:rgba(248,81,73,.3);background:rgba(248,81,73,.1);color:var(--red)" onclick="checkProxy('${escHtml(p.server)}',${i})">↺ Yeniden test</button>`;
    }
    const protoBadge = (p.proto||'http') === 'socks5'
      ? `<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(139,92,246,.2);color:#a78bfa;font-weight:600">SOCKS5</span>`
      : `<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:rgba(56,189,248,.15);color:#38bdf8;font-weight:600">HTTP</span>`;
    const isActive = p.active !== false;
    const toggleBtn = isActive
      ? `<button class="rot-btn" style="border-color:rgba(248,81,73,.4);background:rgba(248,81,73,.12);color:#f85149" onclick="toggleProxy('${escHtml(p.server)}',${i})">⏸ Durdur</button>`
      : `<button class="rot-btn" style="border-color:rgba(35,134,54,.4);background:rgba(35,134,54,.12);color:#3fb950" onclick="toggleProxy('${escHtml(p.server)}',${i})">▶ Etkinleştir</button>`;
    const rowStyle = isActive ? '' : 'opacity:0.45;';
    return `<tr id="proxy-row-${i}" style="${rowStyle}">
      <td>${statusCell}</td>
      <td><b style="color:var(--blue)">${escHtml(p.label||p.server)}</b> ${protoBadge}</td>
      <td style="font-family:monospace;font-size:12px">${escHtml(p.host)}</td>
      <td style="color:var(--muted)">${escHtml(p.port)}</td>
      <td style="color:var(--muted)">${escHtml(p.user||'-')}</td>
      <td>
        ${p.rotation_url
          ? `<span class="rot-url" title="${escHtml(p.rotation_url)}">${escHtml(p.rotation_url.substring(0,35))}...</span><br>
             <button class="rot-btn" id="rotbtn-${i}" onclick="rotateIp('${escHtml(p.server)}','${escHtml(p.rotation_url)}',${i})">⟳ IP Değiştir</button>
             <div class="rot-status" id="rot-status-${i}"></div>`
          : `<button class="btn btn-blue" style="padding:3px 8px;font-size:10px" onclick="editRotUrl('${escHtml(p.server)}')">+ Rotasyon URL</button>`
        }
      </td>
      <td style="white-space:nowrap">
        ${toggleBtn}
        <button class="btn btn-red"  style="padding:3px 8px;font-size:10px;margin-left:4px" onclick="deleteProxy('${escHtml(p.server)}')">🗑</button>
      </td>
    </tr>`;
  }).join('');
}

// Tek proxy test
let _statusMap = {};
async function checkProxy(server, idx){
  // Satırda "test ediliyor" göster
  const badge = document.getElementById('pst-'+idx);
  if(badge){ badge.className='prx-badge checking'; badge.textContent='⏳ Test ediliyor...'; }
  _statusMap[server] = {checking: true};

  const r = await fetch('/api/proxies/check', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({server})
  });
  const data = await r.json();
  const res = data[server] || {ok:false, msg:'Yanıt yok'};
  _statusMap[server] = res;

  if(badge){
    if(res.ok){
      badge.className='prx-badge online';
      badge.textContent=`✅ ${res.ms}ms`;
    } else {
      badge.className='prx-badge offline';
      badge.textContent=`❌ ${res.msg||'Hata'}`;
    }
  }
}

// Tüm proxy'leri test et
async function checkAllProxies(){
  const status = document.getElementById('checkAllStatus');
  status.textContent = '⏳ Test ediliyor...';
  // Önce hepsini "checking" göster
  _proxyList.forEach((p,i) => {
    _statusMap[p.server] = {checking:true};
  });
  renderProxyTable(_proxyList, _statusMap);

  const r = await fetch('/api/proxies/check', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({})
  });
  const data = await r.json();
  Object.assign(_statusMap, data);
  renderProxyTable(_proxyList, _statusMap);

  const total  = Object.keys(data).length;
  const online = Object.values(data).filter(v=>v.ok).length;
  status.textContent = `✅ ${online}/${total} proxy erişilebilir`;
  status.style.color = online===total ? 'var(--green)' : online>0 ? 'var(--yellow)' : 'var(--red)';
}

async function addProxy(){
  const proto = document.getElementById('px-proto').value;
  const host  = document.getElementById('px-host').value.trim();
  const port  = document.getElementById('px-port').value.trim();
  const user  = document.getElementById('px-user').value.trim();
  const pass  = document.getElementById('px-pass').value.trim();
  const label = document.getElementById('px-label').value.trim();
  const rot   = document.getElementById('px-rot').value.trim();
  if(!host || !port){ alert('Host ve port zorunlu!'); return; }
  const r = await fetch('/api/proxies/add', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host,port,user,password:pass,label,rotation_url:rot,proto})
  });
  const d = await r.json();
  alert(d.msg);
  document.getElementById('addProxyForm').style.display='none';
  ['px-host','px-port','px-user','px-pass','px-label','px-rot'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('px-proto').value='http';
  loadProxies();
}

async function toggleProxy(server, idx){
  const r = await fetch('/api/proxies/toggle', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({server})
  });
  const d = await r.json();
  loadProxies();
}

async function deleteProxy(server){
  if(!confirm(`"${server}" silinsin mi?`)) return;
  const r = await fetch('/api/proxies/delete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({server})
  });
  const d = await r.json();
  loadProxies();
}

async function editRotUrl(server){
  const url = prompt(`"${server}" için IP rotasyon URL'sini girin:\n(örn: https://api.provider.com/rotate?token=xxx)`,'');
  if(url === null) return;
  const label = prompt('Etiket (opsiyonel):', server) || server;
  await fetch('/api/proxies/rotation', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({server, url, label})
  });
  loadProxies();
}

async function rotateIp(server, rotUrl, idx){
  const btn = document.getElementById('rotbtn-'+idx);
  const status = document.getElementById('rot-status-'+idx);
  btn.classList.add('loading');
  btn.textContent = '⏳ Değiştiriliyor...';
  status.textContent = '';
  try {
    const r = await fetch('/api/proxies/rotate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({server, url: rotUrl})
    });
    const d = await r.json();
    if(d.ok){
      btn.textContent = '✅ Değiştirildi';
      status.textContent = 'Yeni IP: ' + (d.new_ip || '?');
      status.style.color = 'var(--green)';
    } else {
      btn.textContent = '❌ Hata';
      status.textContent = d.msg || 'İstek başarısız';
      status.style.color = 'var(--red)';
    }
  } catch(e){
    btn.textContent = '❌ Hata';
    status.textContent = e.message;
    status.style.color = 'var(--red)';
  }
  btn.classList.remove('loading');
  setTimeout(()=>{ btn.textContent='⟳ IP Değiştir'; }, 5000);
}

function editProxy(idx){
  // Rotasyon URL düzenleme
  const row = document.getElementById('proxyBody').children[idx];
  const server = row.children[0].textContent.trim();
  editRotUrl(server);
}

// ── Bot actions ──
async function botAction(action){
  if(action==='stop' && !confirm('Botu durdurmak istediğinizden emin misiniz?')) return;
  const r = await fetch('/api/bot/'+action, {method:'POST'});
  const d = await r.json();
  alert(d.msg || action + ' tamam');
  setTimeout(refreshAll, 1500);
}

// Initial load + auto-refresh
refreshAll();
loadProxies();
setInterval(refreshAll, 15000);
setInterval(loadProxies, 30000);

// Load existing logs
fetch('/api/logs').then(r=>r.json()).then(lines=>{
  lines.forEach(l=>{ allLogLines.push(l); appendLogLine(l); });
});
</script>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VISABot — Giriş</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d1117;
    color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 48px 40px 40px;
    width: 100%;
    max-width: 380px;
    box-shadow: 0 16px 48px rgba(0,0,0,0.5);
  }
  .logo {
    text-align: center;
    margin-bottom: 32px;
  }
  .logo span {
    font-size: 48px;
  }
  .logo h1 {
    font-size: 22px;
    font-weight: 700;
    color: #58a6ff;
    margin-top: 8px;
  }
  .logo p {
    font-size: 13px;
    color: #8b949e;
    margin-top: 4px;
  }
  label {
    display: block;
    font-size: 13px;
    color: #8b949e;
    margin-bottom: 6px;
    font-weight: 500;
  }
  input {
    width: 100%;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 8px;
    color: #e6edf3;
    font-size: 15px;
    padding: 12px 14px;
    outline: none;
    transition: border-color 0.2s;
    margin-bottom: 20px;
  }
  input:focus { border-color: #58a6ff; }
  button {
    width: 100%;
    background: #238636;
    border: none;
    border-radius: 8px;
    color: #fff;
    font-size: 15px;
    font-weight: 600;
    padding: 12px;
    cursor: pointer;
    transition: background 0.2s;
    margin-top: 4px;
  }
  button:hover { background: #2ea043; }
  .error {
    background: rgba(248,81,73,0.1);
    border: 1px solid rgba(248,81,73,0.4);
    border-radius: 8px;
    color: #f85149;
    font-size: 13px;
    padding: 10px 14px;
    margin-bottom: 20px;
    text-align: center;
  }
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <span>🛂</span>
    <h1>VISABot Dashboard</h1>
    <p>Lütfen giriş yapın</p>
  </div>
  {% if error %}
  <div class="error">⚠️ {{ error }}</div>
  {% endif %}
  <form method="POST" action="/login">
    <label>Kullanıcı Adı</label>
    <input type="text" name="username" autocomplete="username" autofocus required>
    <label>Şifre</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Giriş Yap →</button>
  </form>
</div>
</body>
</html>"""


@app.before_request
def check_auth():
    if request.path in _PUBLIC_PATHS or request.path.startswith("/socket.io"):
        return
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u == DASHBOARD_USER and p == DASHBOARD_PASS:
            session.permanent = True
            session["logged_in"] = True
            next_url = request.args.get("next", "/")
            return redirect(next_url)
        error = "Kullanıcı adı veya şifre hatalı."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    """Render health check + UptimeRobot ping noktası."""
    return ("ok", 200)


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/stats")
def api_stats():
    return jsonify(_stats())


@app.route("/api/chart")
def api_chart():
    return jsonify(_hourly_chart())


@app.route("/api/attempts")
def api_attempts():
    return jsonify(_recent_attempts())


@app.route("/api/errors")
def api_errors():
    return jsonify(_recent_errors())


@app.route("/api/customers")
def api_customers():
    return jsonify(_customers_summary())


@app.route("/api/status")
def api_status():
    pid = _bot_pid()
    return jsonify({
        "running": pid is not None,
        "pid":     pid,
        "uptime":  _bot_uptime(pid),
    })


@app.route("/api/logs")
def api_logs():
    """Son 200 log satırını döndür (sayfa ilk yüklendiğinde)."""
    try:
        lines = LOG_PATH.read_text(errors="replace").splitlines()[-200:]
        result = []
        for line in lines:
            level = "info"
            if "] ERROR:" in line:   level = "error"
            elif "] WARNING:" in line: level = "warning"
            elif "başarılı" in line.lower() or "geçildi" in line.lower(): level = "success"
            result.append({"line": line, "level": level})
        return jsonify(result)
    except Exception:
        return jsonify([])


@app.route("/api/proxies")
def api_proxies():
    return jsonify(_read_proxies())


@app.route("/api/proxies/add", methods=["POST"])
def api_proxy_add():
    from flask import request
    d = request.get_json()
    proxies = _read_proxies()
    server = f"{d['host']}:{d['port']}"
    # Aynı server varsa güncelle
    existing = [p for p in proxies if p["server"] != server]
    proto = d.get("proto", "http")
    if proto not in ("http", "socks5"):
        proto = "http"
    existing.append({
        "host": d["host"], "port": d["port"],
        "user": d.get("user",""), "password": d.get("password",""),
        "proto": proto,
        "server": server, "rotation_url": d.get("rotation_url",""),
        "label": d.get("label", server),
    })
    _write_proxies(existing)
    # Rotasyon URL varsa kaydet
    if d.get("rotation_url"):
        rots = _load_rotation_urls()
        rots[server] = {"url": d["rotation_url"], "label": d.get("label", server)}
        _save_rotation_urls(rots)
    return jsonify({"msg": f"{server} eklendi."})


@app.route("/api/proxies/delete", methods=["POST"])
def api_proxy_delete():
    from flask import request
    d = request.get_json()
    server = d.get("server", "")
    proxies = [p for p in _read_proxies() if p["server"] != server]
    _write_proxies(proxies)
    # Rotasyon kaydını da sil
    rots = _load_rotation_urls()
    rots.pop(server, None)
    _save_rotation_urls(rots)
    return jsonify({"msg": f"{server} silindi."})


@app.route("/api/proxies/toggle", methods=["POST"])
def api_proxy_toggle():
    from flask import request
    d = request.get_json()
    server = d.get("server", "")
    proxies = _read_proxies()
    for p in proxies:
        if p["server"] == server:
            p["active"] = not p.get("active", True)
            new_state = p["active"]
            break
    else:
        return jsonify({"msg": "Proxy bulunamadı."}), 404
    _write_proxies(proxies)
    state_txt = "aktif" if new_state else "devre dışı"
    return jsonify({"msg": f"{server} {state_txt} yapıldı.", "active": new_state})


@app.route("/api/proxies/rotation", methods=["POST"])
def api_proxy_rotation():
    from flask import request
    d = request.get_json()
    rots = _load_rotation_urls()
    rots[d["server"]] = {"url": d["url"], "label": d.get("label", d["server"])}
    _save_rotation_urls(rots)
    return jsonify({"msg": "Rotasyon URL güncellendi."})


@app.route("/api/proxies/rotate", methods=["POST"])
def api_proxy_rotate():
    """Verilen URL'ye GET isteği yaparak IP rotasyonu tetikle."""
    import urllib.request
    from flask import request
    d = request.get_json()
    url = d.get("url", "")
    if not url:
        return jsonify({"ok": False, "msg": "URL yok"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "VISABot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(500).decode(errors="replace")
        # Yeni IP'yi yanıttan çekmeye çalış
        import re as _re
        ip_match = _re.search(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b', body)
        new_ip = ip_match.group(1) if ip_match else None
        return jsonify({"ok": True, "new_ip": new_ip, "body": body[:200]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/proxies/check", methods=["POST"])
def api_proxy_check():
    """Tek proxy veya tüm proxy'leri TCP bağlantı testi yap."""
    import socket
    from flask import request
    d = request.get_json() or {}
    server = d.get("server")  # tek proxy; boşsa hepsini test et

    proxies = _read_proxies()
    if server:
        proxies = [p for p in proxies if p["server"] == server]

    results = {}
    for p in proxies:
        host = p["host"]
        try:
            port = int(p["port"])
        except Exception:
            results[p["server"]] = {"ok": False, "ms": None, "msg": "Geçersiz port"}
            continue
        t0 = time.time()
        try:
            sock = socket.create_connection((host, port), timeout=6)
            sock.close()
            ms = round((time.time() - t0) * 1000)
            results[p["server"]] = {"ok": True, "ms": ms, "msg": f"{ms}ms"}
        except socket.timeout:
            results[p["server"]] = {"ok": False, "ms": None, "msg": "Timeout (6s)"}
        except ConnectionRefusedError:
            results[p["server"]] = {"ok": False, "ms": None, "msg": "Bağlantı reddedildi"}
        except Exception as e:
            results[p["server"]] = {"ok": False, "ms": None, "msg": str(e)[:60]}

    return jsonify(results)


@app.route("/api/bot/<action>", methods=["POST"])
def api_bot(action):
    import subprocess
    base = str(BASE_DIR)
    if action == "start":
        if _bot_pid():
            return jsonify({"msg": "Bot zaten çalışıyor, yeni instance başlatılmadı."})
        subprocess.Popen(
            ["python3", "visa_watch_onefile.py"],
            cwd=base, stdout=open(LOG_PATH, "a"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return jsonify({"msg": "Bot başlatıldı."})
    elif action == "stop":
        pid = _bot_pid()
        if pid:
            os.kill(pid, 15)
            return jsonify({"msg": f"Bot durduruldu (PID {pid})."})
        return jsonify({"msg": "Bot zaten durdurulmuş."})
    elif action == "restart":
        pid = _bot_pid()
        if pid:
            os.kill(pid, 15)
            # Watchdog'un devreye girmeden önce process'in tamamen kapanmasını bekle
            for _ in range(10):
                time.sleep(0.5)
                if not _bot_pid():
                    break
        # Çift instance önlemi: watchdog bu sürede başlatmış olabilir
        if _bot_pid():
            return jsonify({"msg": "Bot zaten çalışıyor (watchdog başlattı)."})
        subprocess.Popen(
            ["python3", "visa_watch_onefile.py"],
            cwd=base, stdout=open(LOG_PATH, "a"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return jsonify({"msg": "Bot yeniden başlatıldı."})
    return jsonify({"msg": "Bilinmeyen işlem."}), 400


# ──────────────────────────────────────────────────────────────────────────────
# SocketIO events
# ──────────────────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    with _log_lock:
        for line in _log_buffer:
            level = "info"
            if "] ERROR:" in line:    level = "error"
            elif "] WARNING:" in line: level = "warning"
            elif "başarılı" in line.lower() or "geçildi" in line.lower(): level = "success"
            emit("log_line", {"line": line, "level": level})


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=_tail_log, daemon=True)
    t.start()
    _port = int(os.environ.get("PORT", "8585"))
    print(f"🚀 Dashboard başlatılıyor → http://0.0.0.0:{_port}")
    socketio.run(app, host="0.0.0.0", port=_port, debug=False, allow_unsafe_werkzeug=True)
