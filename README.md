# VISABot — Otomatik Vize Randevu Botu

Telegram üzerinden çalışan, **VFS Global**, **AS-Visa** ve **BLS** sistemleri için otomatik slot takip ve randevu alma botu. Türkiye'deki vize başvuru ajansları için geliştirilmiştir.

---

## Desteklenen Servisler

### VFS Global (Camoufox + Hesap Rotasyonu)

| Ülke | Site Key |
|------|----------|
| Fransa 🇫🇷 | `vfs_fr` |
| Hollanda 🇳🇱 | `vfs_nl` |
| Danimarka 🇩🇰 | `vfs_dk` |
| İsveç 🇸🇪 | `vfs_se` |
| Avusturya 🇦🇹 | `vfs_at` |
| Belçika 🇧🇪 | `vfs_be` |
| Çek Cumhuriyeti 🇨🇿 | `vfs_cz` |
| Estonya 🇪🇪 | `vfs_ee` |
| Finlandiya 🇫🇮 | `vfs_fi` |
| İsviçre 🇨🇭 | `vfs_ch` |
| İzlanda 🇮🇸 | `vfs_is` |
| Letonya 🇱🇻 | `vfs_lv` |
| Litvanya 🇱🇹 | `vfs_lt` |
| Lüksemburg 🇱🇺 | `vfs_lu` |
| Malta 🇲🇹 | `vfs_mt` |
| Norveç 🇳🇴 | `vfs_no` |
| Polonya 🇵🇱 | `vfs_pl` |

### AS-Visa (Playwright Stealth)

| Şehir | Site Key |
|-------|----------|
| Macaristan – İstanbul 🇭🇺 | `hu_asvisa_istanbul` |
| Macaristan – Ankara 🇭🇺 | `hu_asvisa_ankara` |

### BLS (Playwright Stealth)

| Ülke | Site Key |
|------|----------|
| İspanya 🇪🇸 | `es_bls` |

---

## Telegram Komutları

| Komut | Açıklama |
|-------|----------|
| `/start` | Ana menüyü açar |
| `/menu` | Ana menüyü açar (alias) |
| `/check` | Ana menüyü açar (alias) |
| `/musteriler` | Müşteri yönetim paneli |
| `/istatistik` | Son 24 saatin başarı istatistikleri |
| `/takip` | Aktif takip oturumları |
| `/restart` | Botu yeniden başlatır |
| Metin yaz | Müşteri adı/pasaport ile otomatik arama |

---

## Bot Menü Akışı

```
/start
└── 🌍 Ülke Seç (country grubu)
    └── 📅 Randevu Al
        ├── Mevcut Başvuru Seç
        └── ➕ Yeni Başvuru Başlat
            ├── Merkez Seç
            ├── Kategori / Alt Kategori Seç
            ├── Tarih & Saat Seç
            └── ✅ Randevu Oluştur / 🔄 Başka Merkez

/musteriler
├── ➕ Yeni Müşteri Ekle       (adım adım wizard)
├── 📋 Tüm Müşteriler
├── ⏳ Bekleyenler
├── ✅ Randevu Alınanlar
├── 🤖 Otomatik Takipler       (aktif oturumlar, durdur)
└── 📊 İstatistik

Müşteri Detay
├── 📅 Randevu Al
├── 🤖 Otomatik Takip Başlat / Durdur
├── ✏️ Durum Güncelle
└── 🗑️ Sil
```

---

## Dashboard

Web tabanlı izleme paneli — **http://localhost:8585**

### Özellikler

- **Bot durumu** — Çalışıyor / Durdu, uptime
- **İstatistik kartları** — Başarılı (24s), Başarı oranı, Toplam deneme, Aktif proxy
- **Saatlik grafik** — Son 7 günün başarı trendi
- **Son denemeler** — Proxy, hesap, süre, sonuç
- **Müşteri özeti** — Bekleyen / tamamlanan / toplam
- **Son hatalar** — Hata mesajı + zaman
- **Proxy yönetimi** — Aktif/pasif toggle, rotation URL düzenleme
- **Canlı log** — WebSocket ile anlık log akışı

### Çalıştırma

```bash
python dashboard.py
# → http://0.0.0.0:8585
```

---

## Proje Yapısı

```
visabotreal/
├── visa_watch_onefile.py   # Ana bot — 1300+ satır
│                           # VFS/AS-Visa/BLS akışları, Telegram handler'lar
├── customer_db.py          # SQLite müşteri modülü
│                           # customers.db — ad, pasaport, durum, ülke
├── optimizer.py            # Akıllı proxy/hesap optimizasyon motoru
│                           # attempt_log, account_cooldown tabloları
├── dashboard.py            # Flask + SocketIO web dashboard (port 8585)
├── watchdog.py             # Bot sağlık izleyici, çökünce yeniden başlatır
├── auto_booking.py         # Otomatik randevu yardımcı modülü
│
├── sites.json              # Site konfigürasyonu [GİTİGNORE]
├── proxies.txt             # Proxy listesi [GİTİGNORE]
├── requirements.txt        # Python bağımlılıkları
├── visa-onefile.service    # Systemd service dosyası
├── start_all.sh            # Bot + dashboard toplu başlatma
│
└── data/                   # Çalışma verisi [GİTİGNORE]
    ├── state.json          # Aktif takip durumları
    ├── customers.db        # Müşteri veritabanı
    ├── cookies/            # VFS oturum cookie'leri
    └── proxy_rotation.json # Proxy başarı istatistikleri
```

---

## Docker ile Kurulum (Alternatif)

Docker kullanmak isteyenler için hazır image GitHub Container Registry'de yayınlanır.

### Hızlı Başlangıç

```bash
# 1. Konfigürasyon dosyalarını oluştur
cp sites.json.example sites.json   # düzenle
cp proxies.txt.example proxies.txt # düzenle
echo "TELEGRAM_BOT_TOKEN=your_token" > .env

# 2. Bot + Dashboard'u başlat
docker compose up -d

# 3. Logları takip et
docker logs -f visabot
```

### Sadece Bot

```bash
docker run -d \
  --name visabot \
  --restart unless-stopped \
  -v $(pwd)/sites.json:/app/sites.json:ro \
  -v $(pwd)/proxies.txt:/app/proxies.txt:ro \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/data:/app/data \
  ghcr.io/kaven0667/visarealbot:latest
```

### Volume Yapısı

| Host | Container | Açıklama |
|------|-----------|----------|
| `./sites.json` | `/app/sites.json` | Site & hesap konfigürasyonu |
| `./proxies.txt` | `/app/proxies.txt` | Proxy listesi |
| `./.env` | `/app/.env` | Telegram token |
| `./data/` | `/app/data/` | State, veritabanı, cookie'ler |

> GOST binary image içinde gelir — SOCKS5 proxy'ler Docker kurulumunda da çalışır.

---

## SOCKS5 Proxy — GOST Köprüsü

Firefox/Camoufox, kimlik doğrulamalı (`user:pass`) SOCKS5 proxy'leri doğrudan desteklemez.
Bu nedenle bot, **GOST** aracılığıyla yerel bir kimliksiz tünel açar:

```
proxies.txt'deki auth-SOCKS5
        │
        ▼
  GOST (yerel port, ör. 127.0.0.1:14501)   ← kimliksiz
        │
        ▼
  Camoufox / Playwright
```

### GOST Kurulumu

```bash
# En son sürümü indir (Linux amd64)
wget https://github.com/go-gost/gost/releases/latest/download/gost_linux_amd64.tar.gz
tar -xzf gost_linux_amd64.tar.gz
mv gost /home/rootuser/visabotreal/gost
chmod +x /home/rootuser/visabotreal/gost
```

> GOST binary proje dizininde (`./gost`) olmalıdır. Binary hassas veri içermez, git'e eklenebilir ya da `.gitignore`'da tutulabilir.

### proxies.txt SOCKS5 Formatı

```
# HTTP proxy (auth doğrudan desteklenir)
ip:port:user:pass:http

# SOCKS5 proxy (GOST köprüsü otomatik açılır)
ip:port:user:pass:socks5

# Kimliksiz SOCKS5
ip:port:::socks5
```

Bot başlarken GOST binary yoksa uyarı verir ve sadece HTTP proxy ile çalışmaya devam eder.
`start_all.sh` da GOST varlığını kontrol eder ve eski tünel süreçlerini temizler.

---

## Kurulum

### 1. Bağımlılıkları Yükle

```bash
pip install -r requirements.txt
playwright install chromium
python -m camoufox fetch
```

### 2. Konfigürasyon

**`sites.json`** — her site için:

```json
[
  {
    "key": "vfs_fr",
    "group_key": "france",
    "menu_label": "France 🇫🇷",
    "url": "https://visa.vfsglobal.com/tur/en/fra/login",
    "telegram_chat_id": "YOUR_CHAT_ID",
    "accounts": [
      {
        "email": "your@gmail.com",
        "password": "your_password",
        "imap_app_password": "your_imap_app_password"
      }
    ],
    "vfs_options": {
      "center": "Istanbul",
      "category": "Schengen Visa"
    }
  }
]
```

**`proxies.txt`** — her satıra bir proxy:
```
http://user:pass@host:port
socks5://host:port
host:port:user:pass:http
```

**`.env`** — Telegram token:
```
TELEGRAM_BOT_TOKEN=your_token_here
```

### 3. Systemd ile Çalıştırma (Önerilen)

```bash
sudo cp visa-onefile.service /etc/systemd/system/visabot.service
# Servis dosyasında WorkingDirectory ve ExecStart yollarını güncelle
sudo systemctl daemon-reload
sudo systemctl enable visabot.service
sudo systemctl start visabot.service
```

Durum ve log:
```bash
sudo systemctl status visabot.service
tail -f /home/rootuser/visabotreal/bot.log
```

### 4. Manuel Çalıştırma

```bash
# Sadece bot
python visa_watch_onefile.py

# Bot + Dashboard birlikte
bash start_all.sh
```

---

## Mimari

```
Kullanıcı (Telegram)
        │
        ▼
visa_watch_onefile.py  ──────────────────────────────────────────┐
        │                                                         │
        ├─── VFS Global ──► Camoufox (Firefox)                   │
        │                       ├── CF Turnstile bypass          │
        │                       ├── Hesap rotasyonu              │
        │                       ├── Proxy rotasyonu              │
        │                       └── OTP ──► Gmail IMAP           │
        │                                                         │
        ├─── AS-Visa ─────► Playwright Chromium (stealth)        │
        ├─── BLS ──────────► Playwright Chromium (stealth)       │
        │                                                         │
        ├─── customer_db.py ──► SQLite (customers.db)            │
        └─── optimizer.py ───► attempt_log / account_cooldown    │
                                                                  │
dashboard.py  ◄───────────────────────────────────────────────────┘
(Flask + SocketIO, port 8585)
```

### Hesap & Proxy Rotasyon Mantığı

- Rate limit → sonraki hesaba geç, **5 dakika cooldown**
- 5xx sunucu hatası → hesap rotasyonu
- Session expire → cookie dosyası silinir, yeniden login
- VFS'de kayıtlı olmayan email → "VFS'de kayıtlı değil" hatası
- Proxy pool → başarı oranına göre dinamik sıralama

---

## Gereksinimler

```
python-telegram-bot==22.1
playwright==1.53.0
python-dotenv==1.0.1
playwright-stealth
setuptools<81
```

---

## Screenshots

> Bot ve dashboard ekran görüntüleri için `screenshots/` klasörüne bakın.

---

## Notlar

- `sites.json`, `proxies.txt` ve `data/` git'e dahil edilmez (hassas bilgi)
- Çoklu VFS hesabı desteklenir — `accounts` listesine ekle
- Dashboard için Flask ve flask-socketio kurulumu gerekir: `pip install flask flask-socketio`
- VFS'den gelen OTP kodları Gmail IMAP üzerinden otomatik okunur; 2FA bildirimleri Telegram üzerinden iletilir

---

## İletişim

Sorular ve destek için Telegram: [@kaven667](https://t.me/kaven667)

---

## Lisans

MIT Lisansı - Telif Hakkı (c) 2026 kaven0667

Ayrıntılar için [LICENSE](LICENSE) dosyasına bakın.
