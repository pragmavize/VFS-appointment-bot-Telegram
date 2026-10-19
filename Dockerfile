# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Camoufox (Firefox) tarayıcısını ÖNCELİKLİ olarak indir.
# Build sırasında GitHub API rate-limit'e takılmamak için GITHUB_TOKEN gerekir
# (Render env değişkeni build arg olarak iletilir; token final image'da kalmaz).
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.53.0-noble AS browser

WORKDIR /tmp/browser
RUN pip install --no-cache-dir camoufox[geoip]

ARG GITHUB_TOKEN
ENV XDG_CACHE_HOME=/tmp/browser/.cache
RUN GITHUB_TOKEN=$GITHUB_TOKEN python -m camoufox fetch
# GeoIP veritabanı (runtime'da indirilmesin diye build'de çek)
RUN python -c "from camoufox.geolocation import download_mmdb; download_mmdb()"

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Runtime
# ─────────────────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

WORKDIR /app

# Python bağımlılıkları
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt camoufox

# fpgen modelini build'de indir (runtime'da webdl timeout'suz takılabilir)
RUN set -eux; \
    DATA_DIR=$(python -c "from camoufox.fpgen_model import fpgen_data_dir; print(fpgen_data_dir())"); \
    mkdir -p "$DATA_DIR"; \
    curl -fSL --retry 5 --retry-delay 5 --max-time 300 \
      -o /tmp/fpgen-model.zip \
      "https://github.com/scrapfly/fingerprint-generator/releases/download/model-2/2026/model-release.zip"; \
    python -c "import zipfile,sys; zipfile.ZipFile('/tmp/fpgen-model.zip').extractall(sys.argv[1])" "$DATA_DIR"; \
    rm -f /tmp/fpgen-model.zip; \
    python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"

# Camoufox tarayıcısı (stage 1'den — runtime'da tekrar indirmez)
COPY --from=browser /tmp/browser/.cache /app/.cache

# GOST binary (SOCKS5 proxy köprüsü)
RUN curl -fsSL https://github.com/go-gost/gost/releases/download/v3.0.0-rc10/gost_3.0.0-rc10_linux_amd64.tar.gz \
    -o /tmp/gost.tar.gz \
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
COPY start_render.sh .
RUN chmod +x start_render.sh

# data/ klasörü (volume mount noktası)
RUN mkdir -p data/cookies

# Camoufox cache yolu — build ve runtime birebir aynı olsun
ENV XDG_CACHE_HOME=/app/.cache
ENV PYTHONUNBUFFERED=1

# Dışarıdan verilecek dosyalar:
#   /app/sites.json    — site & hesap konfigürasyonu (env: SITES_JSON)
#   /app/proxies.txt   — proxy listesi (env: PROXIES_TXT)
#   /app/data/         — state, veritabanı, cookie'ler

CMD ["python3", "visa_watch_onefile.py"]
