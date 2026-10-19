# VISABot — Render Kurulum Rehberi

Bu proje Render'da **tek bir web servisi** olarak çalışır:

- **Bot** (`visa_watch_onefile.py`) → arka planda çalışır (Telegram long-polling).
- **Dashboard** (`dashboard.py`) → Render'ın verdiği `$PORT` üzerinde yayın yapar.

`start_render.sh`, `sites.json` / `proxies.txt` dosyalarını **ortam değişkenlerinden**
okuyup diske yazar, botu arka planda başlatır ve dashboard'u ön planda çalıştırır.

---

## 1. Gerekli ENV değişkenleri

Render Dashboard → servis → **Environment** sekmesinde aşağıdaki değişkenleri ekle
(boş bıraktıklarını kendin doldur). `render.yaml` içinde `sync: false` olanlar
otomatik olarak senin doldurman için listelenir.

### Zorunlu / senin dolduracağın

| Değişken | Ne işe yarar | Örnek |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot tokeni (@BotFather) | `8840036879:AAG...` |
| `ALLOWED_USERS` | Bota erişebilecek Telegram **kullanıcı ID**'leri (virgülle) | `123456789,987654321` |
| `ADMIN_CHAT_ID` | Sistem mesajları/ekran görüntüleri gidecek Telegram ID | `123456789` |
| `SITES_JSON` | Takip edilecek VFS/AS-Visa/BLS hesapları (JSON) | `sites.json.example` içeriği |
| `DASHBOARD_USER` | Dashboard kullanıcı adı | `benimuser` |
| `DASHBOARD_PASS` | Dashboard şifresi | `guclu-sifre` |
| `TELEGRAM_CHAT_ID` | (Kodda kullanılmıyor; istersen doldur) | — |

### Opsiyonel

| Değişken | Ne işe yarar | Varsayılan |
|---|---|---|
| `PROXIES_TXT` | Proxy listesi (her satıra bir tane) | boş (proxy'siz) |
| `WATCHDOG_NOTIFY_ID` | Watchdog bildirimlerinin gideceği Telegram ID | `5816716319` |
| `LOG_LEVEL` | Log seviyesi | `INFO` |

### Render'ın kendisi verdiği (ekleme)

| Değişken | Ne işe yarar |
|---|---|
| `PORT` | Dashboard'un bağlanacağı port (otomatik) |
| `PYTHONUNBUFFERED` | Logların anında akması için (`1`) |

> **Not:** `TELEGRAM_CHAT_ID` koddaki tek kullanımı `visa_watch_onefile.py:138`'de ve
> hiçbir yere bağlanmıyor; yani zorunlu değil. Bota erişim için asıl önemli olan
> `ALLOWED_USERS` ve `ADMIN_CHAT_ID`'dir.

---

## 2. `SITES_JSON` nasıl doldurulur?

1. Repodaki `sites.json.example` dosyasını aç.
2. İçindeki örnekleri kendi VFS hesaplarınla değiştir
   (`email`, `password`, `imap_app_password`).
3. Gereksiz siteleri `"enabled": false` yap veya sil.
4. Tüm JSON'u **tek satır** hâline getir (Render env değişkeni çok satırlı da olur,
   ama tek satır daha güvenli).
5. Render Dashboard → Environment → `SITES_JSON` → yapıştır → **Save Changes**.

### Telegram kullanıcı ID nasıl öğrenilir?

- Telegram'da [@userinfobot](https://t.me/userinfobot) adresine yaz → ID'ni gösterir.
- Ya da botuna bir mesaj at, sonra tarayıcıdan:
  `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini aç → `"chat":{"id":...}`.

> ⚠️ Botun kendi ID'si (`8840036879`) senin kullanıcı ID'n **değildir**.
> `ALLOWED_USERS` / `ADMIN_CHAT_ID` için kendi Telegram kullanıcı ID'ni kullan.

---

## 3. Deploy (API ile kuruldu)

Bu servis Render API üzerinden `kaven0667/VFS-appointment-bot-Telegram`
(veya fork'u) reposundan oluşturuldu. Yeni bir servis kurmak istersen:

1. Render Dashboard → **New** → **Blueprint** → repo'yu seç → `render.yaml` otomatik okunur.
2. Veya **New** → **Web Service** → repo → **Language: Docker** →
   **Docker Command:** `bash start_render.sh` → **Plan: free** → Create.

---

## 4. UptimeRobot ile ayakta tutma (free plan için)

Ücretsiz plan 15 dakika trafik gelmezse servisi uyutur. Bunu önlemek için:

1. [https://uptimerobot.com](https://uptimerobot.com)'a gir → **+ New Monitor**.
2. Type: **HTTP(s)**, Name: `visabot`.
3. URL: servisin `https://<ad>.onrender.com/healthz`
   (Dashboard URL'sinin sonuna `/healthz` ekle).
4. Monitoring Interval: **5 minutes**.
5. Save.

> `/healthz` dashboard tarafından herkese açık (login gerektirmeyen) bir endpoint'tir.

---

## 5. Önemli uyarılar

- **RAM:** Free plan 512 MB'dır. Camoufox (Firefox) yüzünden yetmeyebilir.
  Servis loglarında `OOMKilled` / çökme görürsen planı **Starter (1c-2g)** veya
  üzerine çıkar.
- **Veri kalıcılığı:** Free plan'da kalıcı disk yoktur. Redeploy sonrası
  `data/` (müşteriler, cookie'ler, state) sıfırlanır. Kalıcı disk için ücretli plan.
- **Güvenlik:** `DASHBOARD_USER` / `DASHBOARD_PASS` varsayılanı (`kaven667` /
  `botan12K!`) ile gelir — mutlaka değiştir.
- **socks5 proxy:** `proxies.txt`'te `socks5` varsa bot, image içindeki `gost`
  binary'siyle yerel tünel açar. Gerek yoksa proxy'siz de çalışır.
