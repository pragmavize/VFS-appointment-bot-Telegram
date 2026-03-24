# VFS Visa Bot 🤖

Telegram üzerinden çalışan, VFS Global ve diğer vize randevu sistemleri için otomatik slot takip ve rezervasyon botu.

---

## Özellikler

- **VFS Global** — Camoufox (Firefox tabanlı, Cloudflare Turnstile bypass) + hesap rotasyonu + proxy rotasyonu
- **AS-Visa / BLS** — Playwright Chromium + stealth modu
- **OTP otomasyonu** — Gmail IMAP üzerinden otomatik OTP okuma
- **Müşteri yönetimi** — SQLite tabanlı müşteri veritabanı (`/musteriler` komutu)
- **Akıllı optimizasyon** — Proxy/hesap başarı oranı takibi, otomatik cooldown
- **Systemd entegrasyonu** — Otomatik başlangıç ve çökme sonrası yeniden başlatma
- **Dashboard** — Anlık durum izleme

---

## Proje Yapısı

```
visabotreal/
├── visa_watch_onefile.py   # Ana bot (1300+ satır)
├── customer_db.py          # Müşteri veritabanı modülü
├── optimizer.py            # Proxy/hesap optimizasyon motoru
├── dashboard.py            # Durum dashboard'u
├── watchdog.py             # Bot sağlık izleyici
├── auto_booking.py         # Otomatik rezervasyon yardımcısı
├── sites.json              # Site konfigürasyonu (gitignore'da)
├── proxies.txt             # Proxy listesi (gitignore'da)
├── requirements.txt        # Python bağımlılıkları
├── visa-onefile.service    # Systemd service dosyası
├── start_all.sh            # Toplu başlatma scripti
└── data/                   # Durum dosyaları (gitignore'da)
    ├── state.json
    ├── customers.db
    └── cookies/
```

---

## Kurulum

### 1. Gereksinimler

- Python 3.10+
- Playwright
- Camoufox

```bash
pip install -r requirements.txt
playwright install chromium
python -m camoufox fetch
```

### 2. Konfigürasyon

**`sites.json`** dosyasını oluştur (şablonu kopyala):

```bash
cp sites.json.example sites.json
```

Her site için gerekli alanlar:
- `key` — site tanımlayıcısı
- `url` — hedef URL
- `accounts` — VFS hesap listesi (email, şifre, IMAP app password)
- `telegram_chat_id` — bildirim alacak Telegram chat ID

**`proxies.txt`** dosyasına proxy ekle:
```
http://user:pass@host:port
socks5://host:port
```

**`.env`** dosyası (Telegram bot token):
```
TELEGRAM_BOT_TOKEN=your_token_here
```

### 3. Systemd ile Çalıştırma

```bash
sudo cp visa-onefile.service /etc/systemd/system/visabot.service
sudo systemctl daemon-reload
sudo systemctl enable visabot.service
sudo systemctl start visabot.service
```

Log takibi:
```bash
tail -f /home/rootuser/visabotreal/bot.log
```

### 4. Manuel Çalıştırma

```bash
python visa_watch_onefile.py
```

---

## Telegram Komutları

| Komut | Açıklama |
|-------|----------|
| `/start` | Ana menü |
| `/musteriler` | Müşteri paneli (ekle, listele, ara) |
| `/istatistik` | Son 24 saatin başarı istatistikleri |
| Metin yaz | Otomatik müşteri arama |

---

## Mimari

```
Telegram Bot API
      │
      ▼
visa_watch_onefile.py
      │
      ├── VFS Global → Camoufox (Firefox, CF bypass)
      │                    └── OTP → Gmail IMAP
      │
      ├── AS-Visa / BLS → Playwright Chromium (stealth)
      │
      ├── customer_db.py → SQLite (customers.db)
      │
      └── optimizer.py → attempt_log, account_cooldown tabloları
```

### Hesap & Proxy Rotasyonu

- Rate limit → bir sonraki hesaba geç, 5 dk cooldown
- 5xx sunucu hatası → hesap rotasyonu
- Proxy pool → başarı oranına göre sıralanır

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

## Notlar

- `sites.json`, `proxies.txt` ve `data/` klasörü hassas bilgi içerdiğinden git'e dahil edilmez
- Çoklu VFS hesabı desteklenir (hesap rotasyonu için `accounts` listesi)
- Session expire olduğunda cookie otomatik temizlenir ve yeniden login yapılır

---

## Lisans

Özel kullanım. İzinsiz dağıtım yasaktır.
