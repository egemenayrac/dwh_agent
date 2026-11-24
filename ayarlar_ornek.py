# -*- coding: utf-8 -*-
"""ayarlar_ornek.py - Kuruma özel ayarların şablonu.

Bu dosyayı aynı klasörde "ayarlar.py" adıyla kopyala ve değerleri doldur.
ayarlar.py GitHub'a yüklenmez (.gitignore), kod dosyalarını güncellediğinde de silinmez.
Şifre ve API anahtarı BURAYA YAZILMAZ: şifre çalışırken sorulur, API anahtarı arayüzden girilir.
"""

# Oracle
DB_USER = "KULLANICI_KODUN"
DB_HOST = "veri-ambari-sunucusu.sirket.local"
DB_PORT = 1521
DB_SERVICE = "servis_adi"

# Bilgi tabanının yazılacağı klasör (yerel disk önerilir)
KB_DIR = r"C:\Users\KULLANICI_KODUN\Desktop\agent\kb"

# Taranacak klasörler (ortak alan UNC yolu olabilir)
CRAWL_ROOTS = [
    r"\\SUNUCU\ORTAK_ALAN\KLASOR",
]

# LLM (OpenAI uyumlu chat/completions)
LLM_URL = "https://llm-sunucusu.sirket.local/v1/chat/completions"
LLM_MODEL = "model-adi"
API_KEY_PAGE = "llm-portali.sirket.local/api-keys"

# İstersen kb_builder'daki diğer ayarları da burada değiştirebilirsin, ör.:
# META_MAX_AGE_DAYS = 30
# PERSONAL_REGEX = r"^[ASTU]\d{3,}"

# Şirket inceleme ajanı (inceleme.py)
# EWS_DIR = r"G:\...\news_ingestion"          # llm_dogrulama.xlsx ve portfolio_config.py'nin bulunduğu klasör
# DIS_PROXY = "http://proxy-sunucusu:8080"     # ilan.gov.tr / Google News / KAP için (boşsa doğrudan bağlanır)
