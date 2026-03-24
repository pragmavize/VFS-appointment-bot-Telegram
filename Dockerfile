# Playwright resmi image — browser bağımlılıkları hazır gelir
FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

WORKDIR /app

# Python bağımlılıkları
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Camoufox (Firefox)
RUN python -m camoufox fetch

# GOST binary (SOCKS5 proxy köprüsü)
RUN wget -q https://github.com/go-gost/gost/releases/download/v3.0.0-rc10/gost_3.0.0-rc10_linux_amd64.tar.gz \
    -O /tmp/gost.tar.gz \
    && tar -xzf /tmp/gost.tar.gz -C /tmp \
    && mv /tmp/gost /app/gost \
    && chmod +x /app/gost \
    && rm /tmp/gost.tar.gz

# Uygulama dosyaları
COPY visa_watch_onefile.py .
COPY customer_db.py .
COPY optimizer.py .
COPY dashboard.py .
COPY watchdog.py .
COPY auto_booking.py .

# data/ klasörü (volume mount noktası)
RUN mkdir -p data/cookies

# Dışarıdan mount edilecek dosyalar:
#   /app/sites.json    — site & hesap konfigürasyonu
#   /app/proxies.txt   — proxy listesi
#   /app/.env          — TELEGRAM_BOT_TOKEN
#   /app/data/         — state, veritabanı, cookie'ler

CMD ["python3", "visa_watch_onefile.py"]
