# DWH Ajanı

Oracle veri ambarı için yapay zekâ ajanı. Veri ambarını keşfeder ve belgeler, ekibin geçmiş SQL dosyalarından
öğrenir, Türkçe soruları SQL'e çevirip çalıştırır, sonucu açıklar ve Excel olarak verir.

Model olarak kurum içi, OpenAI uyumlu bir LLM servisi (native tool calling) kullanır. Veri kurum dışına çıkmaz.

Ayrıntılı mimari ve tasarım kararları: [docs/mimari.md](docs/mimari.md)

## Nasıl çalışır

**1. Bilgi tabanı (çevrimdışı, haftalık)**

| Script | Görev |
|---|---|
| `kb_builder.py` | LLM kullanmadan Oracle kataloğunu, view/PL-SQL kodunu ve ortak klasördeki SQL/Python/Excel dosyalarını tarar. Tablo kullanımını, kanıtlı join'leri ve kökeni çıkarır. |
| `kb_profile.py` | Veri okumadan, Oracle istatistiklerinden kolon profillerini ve LOOKUP tablolarından kod anlamlarını çıkarır. |
| `kb_annotate.py` | LLM ile kanıta dayalı tablo ve dosya kartları (dokümantasyon) yazar. |

**2. Ajan (canlı)**

| Dosya | Görev |
|---|---|
| `ajan_core.py` | Araç kullanan ajan döngüsü: bilgi arama, tablo kartı, join yolu, güvenli SQL çalıştırma, tekillik kontrolü, plan onayı, ölçüm. |
| `ajan_app.py` | Streamlit arayüzü: soru-cevap, onay, Excel, mail talebi, ölçüm. |
| `baslat.py` / `durdur.py` | Arayüzü Spyder'dan (F5) başlatır / durdurur. |

## Kurulum

1. Paketler (Spyder konsolunda): `%pip install -r requirements.txt`
2. `ayarlar_ornek.py` dosyasını `ayarlar.py` adıyla kopyala ve kendi değerlerini yaz (Oracle kullanıcı/sunucu,
   taranacak klasörler, LLM adresi). `ayarlar.py` repoya yüklenmez.
3. Sırayla F5 ile çalıştır: `kb_builder.py` → `kb_profile.py` → `kb_annotate.py` (önce `LIMIT_*` ile dene).
4. `baslat.py` ile ajanı aç. LLM API anahtarını ve Oracle şifresini arayüzden gir.

## Güvenlik

- Kod seviyesinde yalnızca tek bir `SELECT`/`WITH` sorgusu çalıştırılır (sqlglot + yasaklı kelime kontrolü).
  Ağır sorgular EXPLAIN PLAN maliyetiyle yakalanır. Önerilen kurulum: ajan için yalnızca SELECT yetkili ayrı bir
  veritabanı hesabı.
- Tek yazma işlemi: kullanıcı açıkça isterse ve ekranda onaylarsa, bir SELECT sonucundan kullanıcının **kendi
  şemasında yeni** bir tablo oluşturulur (`CREATE TABLE ... AS SELECT`). Var olan tablonun üzerine yazılmaz; başka
  şemaya, INSERT/UPDATE/DELETE/DROP yapılamaz. Ölçüm modunda kapalıdır.
- Bilgi tabanı kurulurken LLM'e yalnızca metadata ve SQL metni gider. Kişisel veri olabilecek kolonların değer
  profilleri alınmaz. Loglara veri satırı yazılmaz.
- Şifre ve API anahtarı kodda tutulmaz. Bilgi tabanı (`kb/`) ve çıktılar repoya yüklenmez.
