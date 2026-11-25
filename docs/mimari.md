# Kredi Riski Veri Ambarı Ajanı: Amaç, Mimari ve Tasarım Kararları

> Bu belge başka bir yapay zekâ modelinin sistemi okuyup analiz etmesi için hazırlanmıştır. İç sunucu adları,
> kullanıcı kodları, şifreler, API anahtarları ve ağ yolları bilerek çıkarılmıştır.

---

## 1. Asıl hedef

Bir bankanın **kredi riski ekibi** için, kurum içi Oracle veri ambarı (DWH) üzerinde çalışan bir yapay zekâ
ajanı. Ajandan beklenenler:

1. **Veri ambarını keşfetmek ve belgelemek:** Binlerce tabloyu, ekibin kişisel şemalarını da (A/T/S/U ile başlayan
   kullanıcı şemaları) tanımak. Her tablonun ne tuttuğunu, anahtarlarını ve tarih kolonlarını, nasıl join edildiğini bilmek.
2. **Ekibin bilgisini öğrenmek:** Ortak ağ sürücüsündeki yıllardır birikmiş SQL, Python, Excel, notebook ve metin
   dosyalarını okuyup ekibin tekrarlayan sorgularını ve iş akışlarını çıkarmak. Örnekler: aylık PD eşleme, izleme
   listeleri, geriye dönük skorlama.
3. **Soruları cevaplamak:** "Ekim sonunda 90+ gecikmeli ticari kredi sayısı nedir?" gibi Türkçe bir soruyu SQL'e
   çevirmek, çalıştırmak, sonucu açıklamak ve Excel olarak vermek.
4. **Gelen veri taleplerini işlemek:** Başka birimlerden mail ile gelen talepleri okuyup sorguyu çalıştırmak ve cevap
   taslağı hazırlamak. Göndermek her zaman insanın elindedir.
5. **İleride PD modeli geliştirme** adımlarına yardım etmek (veri hazırlama, değişken analizi; EBA/IRB çerçevesine uygun).

Uzun vadeli vizyon: ekibin "kurumsal hafızası" olan, yeni gelen birinin aylarca öğreneceği tablo ve iş bilgisini
anında kullanabilen bir asistan.

---

## 2. Kısıtlar (tasarımı belirleyen gerçekler)

| Kısıt | Sonucu |
|---|---|
| Model: bankanın kendi LLM servisi (IBThink), **Qwen3 27B**, OpenAI uyumlu `/v1/chat/completions` | Dışarıya veri çıkmaz. Native tool calling çalışıyor. Model küçük ve orta boy, GPT-4/Claude seviyesinde değil. |
| Bağlam 400k token, ama **istek başına çıktı en fazla 4096 token** (düşünme yazısı dahil) | Uzun çıktılar parçalara bölünmeli. Toplu işlerde Qwen'in `<think>` modu kapatılıyor (`/no_think`). |
| Kişisel API anahtarında **paralel istek limiti 1** | Toplu kart yazımı tek işçiyle, gece boyu ve kaldığı yerden devam edecek şekilde çalışır. |
| Oracle 19c, ajan **kullanıcının kendi hesabıyla** bağlanıyor (bu hesabın kendi şemasında yazma yetkisi var), **V$SQL gibi sistem görünümlerine erişim yok** | Sorgu geçmişi veritabanından alınamıyor. Ekibin bilgisi ağ sürücüsündeki dosyalardan çıkarılıyor. |
| İş bilgisayarı: Windows, Anaconda + **Spyder**. **Anaconda Prompt ve terminal kapalı.** Kullanıcı .py dosyası indiremiyor | Her şey Spyder'da F5 ile çalışan saf Python scriptleri. Kod kullanıcıya metin olarak verilip yapıştırılıyor. Kurulum `%pip` ile. |
| Docker, Postgres, sunucu yok | Bilgi tabanı tek bir **SQLite** dosyası. Vektör veritabanı ve graph veritabanı sunucusu yok. |
| Kurumsal TLS (kendi sertifika otoritesi) | `requests` ile Windows sertifika deposundan PEM üretilip `verify=` olarak veriliyor. |
| Müşteri verisi gizliliği | Toplu işlerde LLM'e **sadece metadata ve SQL metni** gider. İstatistik profilinde kişisel olabilecek kolonlar atlanır. Loglara veri satırı yazılmaz. |

---

## 3. Genel mimari

İki ayrı katman var: **(A) çevrimdışı bilgi tabanı kurulumu** (bir kez ya da haftalık çalışır) ve **(B) canlı ajan**
(kullanıcı soru sordukça çalışır).

```
                (A) BİLGİ TABANI KURULUMU (çevrimdışı)
 Oracle katalog ─┐
 View/PLSQL kodu ─┼─> kb_builder.py ──> kb.sqlite (deterministik: tablolar, kolonlar, kullanım, join kanıtları, köken)
 Ağ sürücüsü ────┘          │
                            v
 Oracle istatistik ─> kb_profile.py ──> kolon profilleri + LOOKUP kod anlamları
                            │
                            v
 IBThink LLM ──────> kb_annotate.py ──> dosya kartları, tablo kartları, sözlük (+ insanların okuyacağı .md/.csv)
                            ^
              onayli_bilgiler.txt (ekibin kesinleştirdiği bilgiler)

                (B) CANLI AJAN
 Kullanıcı ─> ajan_app.py (Streamlit) ─> ajan_core.py (ajan döngüsü + araçlar) ─> IBThink LLM
                                               │                 │
                                               v                 v
                                          kb.sqlite         Oracle (sadece SELECT, kod kontrolü)
```

### Dosyalar

| Dosya | Görev |
|---|---|
| `kb_builder.py` | Deterministik bilgi tabanı kurucusu, **LLM kullanmaz**. |
| `kb_profile.py` | Oracle optimizer istatistiklerinden kolon profilleri ve LOOKUP tablolarından kod anlamları. |
| `kb_annotate.py` | LLM ile dosya kartları, tablo kartları ve sözlük yazımı. |
| `ajan_core.py` | Ajanın beyni: LLM istemcisi, araçlar, döngü, güvenlik, log, ölçüm. Arayüzden bağımsız. |
| `ajan_app.py` | Streamlit arayüzü. |
| `baslat.py` / `durdur.py` | Streamlit'i Spyder'dan başlatma ve durdurma (terminal kapalı olduğu için). |

---

## 4. (A) Bilgi tabanı: neden ve nasıl

### 4.1 Temel tercih: önce deterministik, sonra LLM

Literatürde ve endüstride kurumsal text-to-SQL'in en büyük sorunu, modelin **hangi tabloyu kullanacağını ve
nasıl join edeceğini bilmemesi** (Spider 2.0, BIRD; Uber QueryGPT, LinkedIn SQL Bot, Pinterest deneyimleri). Bu
yüzden:

- **Kesin bilinebilen her şey kodla çıkarılır.** Hangi tablo var, hangi kolonlar var, hangi dosyada hangi tablo
  kullanılmış, hangi iki tablo hangi kolonlarla join edilmiş. Bu bilgiler LLM'e tahmin ettirilmez. LLM uydurabilir,
  kod uydurmaz.
- **LLM sadece yorumlama işi yapar:** kanıtları okuyup "bu tablo ne tutar, bu kolon ne anlama gelir" diye özet
  yazar. Yazarken önüne kanıt paketi konur ve uydurmaması istenir.

### 4.2 kb_builder.py (deterministik)

1. **Metadata toplama:** `ALL_TABLES`, `ALL_TAB_COLUMNS`, yorumlar, PK/FK, synonym'ler, satır sayısı istatistikleri.
   Kişisel şemalar da dahil. Şema bazında "en son ne zaman toplandı" kaydı tutulur, belli bir yaştan
   (`META_MAX_AGE_DAYS`) taze olan şemalar atlanır.
2. **Veritabanı kodu:** View metinleri ve PL/SQL kaynak kodu da SQL örneği olarak alınır.
3. **Dosya tarama:** Ağ sürücüsündeki `.sql .py .ipynb .txt .xlsx` (Power Query/M kodu dahil) vb. dosyalar
   okunur, içlerinden SQL parçaları (snippet) çıkarılır. Dosya içi sıra korunur, yorumlar silinmez.
4. **Analiz:** Her snippet için tablo referansları, CTE'ler, alias'lar ve `JOIN ... ON a.x = b.y` eşitlikleri çıkarılır.
5. **Çözümleme (en kritik adım):** Metinde geçen `CASH_LOANS` adının hangi şemadaki tabloya karşılık geldiği bulunur.
   Sırasıyla synonym'e bakılır, isim tek şemada varsa o şema alınır, değilse dosyanın "ev şeması" puanlanır.
   Script'in kendi oluşturduğu geçici tablolar başka şemalara bağlanmaz. Dönemlik tablo aileleri
   (`X_202410`, `X_202411` → `X_{DONEM}`) tek aile olarak tutulur. `CREATE TABLE AS SELECT` zincirleri en fazla
   4 adım geriye izlenir; geçici tablo üzerinden yapılmış bir join gerçek kaynak tablolara taşınır.
6. **Çıktılar:** `join_edges` (her join için kanıt sayısı ve kaynak dosyalar), `v_usage` (tablo kaç dosyada
   kullanılmış), `v_lineage` (köken), FTS5 tam metin indeksi ve gözden geçirme için CSV'ler.

Gelinen nokta: tablo referanslarının yaklaşık %54'ü kesin bir katalog tablosuna bağlanıyor, %82'si açıklanabiliyor
(geri kalanı geçici tablo, CTE vb.). Doğrulanmış join sayısı iyileştirmelerle 189'dan 329'a çıktı.

### 4.3 kb_profile.py (veri okumadan profil)

- Tablolar taranmaz. Oracle'ın zaten tuttuğu **optimizer istatistikleri** okunur: min/max değerler (RAW biçimden
  çözülerek) ve az farklı değerli kod kolonları için frekans histogramları (ör. `STATUS: A %62, K %30`).
- Kişisel olabilecek kolonlar (ad, adres, TCKN, telefon vb.) ve metin kolonlarının aralıkları atlanır.
- **LOOKUP çözümü:** Join kanıtlarından "bu kod kolonu şu LOOKUP tablosuna bağlanıyor" bilgisi çıkarılır. 2000
  satırdan küçük referans tabloları okunarak kodların anlamları alınır (ör. `CREDIT_ST = 8 → ...`). Bunlar
  müşteri verisi değil, referans verisidir.

Neden: LLM'in "bu kolon tarih mi, YYYYMMDD sayı mı, kod mu, kodlar ne anlama geliyor" sorusunu tahminle değil
kanıtla cevaplaması için.

### 4.4 kb_annotate.py (LLM ile kart yazımı)

- **Dosya kartı:** Her script için "ne üretir, hangi adımlarla, hangi tablolardan, parametreleri, iş kuralları,
  üretim mi deneme mi" bilgileri.
- **Tablo kartı:** LLM'e bir **kanıt paketi** verilir: kolonlar ve tipleri, profil, çözülmüş LOOKUP kodları,
  doğrulanmış join'ler, aynı isimli kolon adayları, köken, örnek SQL'ler, tabloyu kullanan dosyaların amaçları.
  Model bundan ne tuttuğunu, satır seviyesini, anahtar ve tarih kolonlarını, önemli kolon anlamlarını, kısaltmaları,
  tuzakları (JSON olarak) yazar.
- **"Nasıl join edilir" bölümü LLM'e yazdırılmaz.** Doğrulanmış join'lerden kodla üretilir. LLM'in önerdiği join'ler
  ayrı bir "Aday join'ler" başlığında, kanıtsız olarak işaretlenir. Her kartta bir **kanıt düzeyi** vardır.
- **Onaylı bilgiler:** `onayli_bilgiler.txt` dosyasında ekibin kesinleştirdiği gerçekler tutulur (ör. "PART_ID
  genellikle YYYYMM biçiminde partition/dönem kolonudur"). Bunlar hem kart yazımında hem canlı ajanda "kesin bilgi"
  olarak kullanılır. Kodun içine gömülü değildir, ekip düzenler.
- **Teknik tercihler:** `max_tokens=4096` ve `/no_think` (aksi halde düşünme bütçeyi yiyip JSON'u yarım bırakıyordu).
  Tek işçi kullanılır. Girdinin hash'i değişmeyen kart tekrar yazılmaz. `MAX_HOURS` ile gece çalışıp durur, ertesi
  gece kaldığı yerden devam eder. `PROMPT_VERSION` değişince kartlar yenilenir.
- **Çıktılar:** SQLite'ta `file_cards`, `table_cards`, `glossary` tabloları, insanların okuyacağı `.md` kartlar ve
  Excel'de gözden geçirilecek CSV özetleri.

Bu yaklaşım "LLM ile metadata zenginleştirme" literatürüyle (ör. arXiv 2503.09003) ve Snowflake/Databricks'in
semantik katman pratikleriyle uyumludur.

### 4.5 Neden vektör veritabanı ve GraphRAG yok (şimdilik)

- **Arama:** SQLite FTS5 (tam metin) ve tablo/kolon adı ile kart içeriği üzerinde puanlama kullanılıyor. Tablo
  adları ve kısaltmalar çok "kelimesel" (CASH_LOAN, DPD, NPL) olduğu için anahtar kelime araması burada güçlü.
  Eş anlamlıları (batık/takip/NPL, gecikme/delay/dpd) modelin kendisi arama kelimesi olarak üretiyor.
- **Graph:** Bilgi tabanı zaten bir graph: tablolar düğüm, kanıtlı join'ler kenar, köken ilişkileri yönlü kenar.
  `networkx` ile iki tablo arasındaki en kısa doğrulanmış join yolu bulunuyor. Bu, GraphRAG fikrinin ihtiyacımız olan
  kısmı. Ayrı bir graph veritabanı ya da graph neural network gerekmedi.
- **Embedding:** Bankada `paraphrase-multilingual-mpnet-base-v2` erişilebilir durumda. Anlamsal arama ileride
  eklenebilir. Önce anahtar kelime aramasının nerede yetersiz kaldığını ölçmek istedik.
- NVIDIA'nın otomatik ontoloji yaklaşımı incelendi. Oracle desteği yok, Docker/Postgres istiyor. Sadece fikir
  kaynağı olarak kullanıldı.

---

## 5. (B) Canlı ajan

### 5.1 Neden hazır framework (LangChain vb.) değil

- Kurulum kısıtları (terminal yok, paket indirme sınırlı) ve kurumsal ağ.
- Model native tool calling destekliyor, döngü ~100 satır Python ile yazılabiliyor.
- **İnsan onayı için durup kaldığı yerden devam etme** gerekiyordu. Streamlit'in "her tıklamada script baştan çalışır"
  modeliyle uyumlu, serileştirilebilir bir oturum nesnesi istedik. Kendi döngümüzü yazmak hem şeffaf hem de hata
  ayıklaması kolay oldu.
- Bağımlılıklar az: `requests`, `oracledb` (thin mode, Oracle client gerekmez), `pandas`, `sqlglot`, `networkx`,
  `streamlit`, `openpyxl`.

### 5.2 Ajan döngüsü (ReAct benzeri, native tool calls)

```
mesajlar = [system_prompt, kullanıcı_sorusu]
döngü (en fazla 30 LLM adımı):
    cevap = LLM(mesajlar, araçlar)
    araç çağrısı yoksa -> metni nihai cevap say, bitir
    her araç çağrısı için:
        cevap_ver ise           -> sonucu kaydet, bitir
        plan_onayla / kullaniciya_sor ise -> DUR, oturumu sakla, kullanıcıdan cevap bekle ("waiting")
        diğer araçlar           -> çalıştır, sonucu (en fazla 9000 karakter) mesajlara ekle
```

- Kullanıcı arayüzde "Onayla" ya da "Düzeltmeyi gönder" dediğinde `resume()` cevabı bekleyen araç çağrısının
  sonucu olarak ekler ve döngü kaldığı yerden devam eder.
- Model 4096 token sınırına takılırsa (`finish_reason == "length"`) yarım çıktı atılır ve `/no_think` ile kısa tekrar
  istenir.
- Her adım (araç adı, girdi, kısaltılmış çıktı) arayüzdeki "Adımlar" panelinde gösterilir. Bu panelde uzun çıktılar
  kısaltılabilir, nihai cevabın tamamı panelin üstünde gösterilir.

### 5.3 Araçlar

| Araç | Ne yapar | Neden var |
|---|---|---|
| `bilgi_ara(kelimeler)` | Tablo adı, yorum ve kart içeriğinde arama yapar. Kullanım sayısına göre sıralar. Ekibin **onaylı sorgularını** ve benzer geçmiş sorguları da döner | Model tablo uydurmasın, önce aday bulsun |
| `tablo_karti(tablo)` | Kolonlar, profil, kod anlamları, kart özeti, doğrulanmış join'ler | Model sadece kartta gördüğü kolonları kullansın |
| `join_yolu(t1, t2)` | networkx ile kanıtlı en kısa join yolu ve ON koşulları | Join tahmin edilmesin (en sık hata kaynağı) |
| `koken(tablo)` | Tablonun hangi tablolardan üretildiği | Türetilmiş ve kaynak tabloyu ayırt etmek için |
| `son_donem(ad)` | Dönemlik tablo ailesinin en güncel dönemi | `X_202410` gibi tablolarda doğru ayı seçmek için |
| `sql_calistir(sql, amac, zorla)` | Güvenlik kontrolü, EXPLAIN PLAN maliyet kontrolü, çalıştırma. Modele ilk 25 satır, ekrana en fazla 5000 satır | Asıl iş. Ağır sorguda önce uyarır |
| `tekillik_kontrol(sql, anahtarlar)` | `COUNT(*)` ile `COUNT(DISTINCT anahtar)` karşılaştırması | Join'den kaynaklanan satır çoğalmasını yakalamak için |
| `plan_onayla(plan)` | Karmaşık sorularda SQL yazmadan önce planı kullanıcıya onaylatır | Yanlış yorumu erkenden yakalamak için (insan döngüde) |
| `kullaniciya_sor(soru)` | Belirsizlik ya da tablo seçimi için kullanıcıya sorar | Uydurmak yerine sormak için |
| `tablo_olustur(tablo_adi, sql, aciklama)` | Kullanıcı açıkça isterse SELECT sonucundan kullanıcının kendi şemasında yeni tablo (CTAS). Ekranda SQL gösterilip onay alınmadan çalışmaz | Analistin "şunu tabloya at" ihtiyacı; kontrollü tek yazma işlemi |
| `cevap_ver(cevap, sql, varsayimlar)` | Nihai cevap, SQL ve varsayımlar | Yapılandırılmış bitiş |

### 5.4 System prompt'un ana kuralları

1. Önce `bilgi_ara`. Türkçe/İngilizce eş anlamlıları ve kısaltmaları da ara.
2. Kullanılacak her tablo için `tablo_karti`. Tablo veya kolon adı uydurma.
3. Join için `join_yolu`. Doğrulanmış yol yoksa kullanıcıya sor.
4. Karmaşık soruda (2+ tablo, çok adımlı hesap, dönem karşılaştırması) önce `plan_onayla`.
5. Emin değilsen `kullaniciya_sor`.
6. Tarihçe/snapshot tablolarında mutlaka tarih/dönem filtresi (AS_OF_DT, PART_ID).
7. Join'li sorgularda `tekillik_kontrol`.
8. Hata gelirse düzelt, sonucun mantıklı olduğunu kontrol et.
9. Sadece anlam soruluyorsa SQL çalıştırma.
10. Kısa Türkçe cevap, nihai SQL ve varsayımlarla `cevap_ver`.

Ayrıca Oracle sözdizimi kuralları verilir (`FETCH FIRST`, `ADD_MONTHS`, YYYYMMDD sayısal tarihler) ve
`onayli_bilgiler.txt` "kesin bilgiler" olarak prompt'a eklenir.

### 5.5 Güvenlik

- **Bağlantı:** Ajan kullanıcının kendi Oracle hesabıyla bağlanıyor. Bu hesabın kendi şemasında tablo oluşturma/yazma
  yetkisi var, yani veritabanı tarafında salt okuma garantisi **yok**; koruma şu an kod katmanında. Hedef: ajan için
  ayrı, sadece SELECT yetkili bir servis hesabı.
- **Kod katmanı:** Sorgu `SELECT`/`WITH` ile başlamalı. `sqlglot` (Oracle diyalekti) ile tek ifade ve okuma türü
  kontrol edilir, ayrıştırılamazsa yorumlar ve string'ler temizlenip `;` ve yasaklı kelimeler regex ile aranır.
- **Tablo oluşturma (tek yazma işlemi):** `tablo_olustur` yalnızca kullanıcının kendi şemasında, var olmayan bir adla
  `CREATE TABLE ... AS SELECT` çalıştırır. SELECT kısmı aynı kontrollerden geçer, tablo adı katı bir kalıpla doğrulanır,
  SQL ekranda gösterilir ve kullanıcı "Onayla"ya basmadan çalışmaz. Ölçüm modunda kapalıdır.
- **Ağır sorgu koruması:** EXPLAIN PLAN maliyeti ya da tahmini satır sayısı eşiği aşarsa çalıştırılmaz, model
  filtre eklemeye ya da kullanıcı onayı almaya yönlendirilir.
- Sorgu zaman aşımı 3 dakika, ekranda en fazla 5000 satır. Tam Excel dışa aktarımı ayrı düğmeyle yapılır (en fazla
  500 bin satır).
- **Log:** Soru, adımlar, SQL ve süre yazılır. **Veri satırı yazılmaz.**
- API anahtarı koda yazılmaz, yerel bir dosyada ya da arayüzde tutulur. Oracle şifresi sadece oturum boyunca
  bellekte durur.
- Mail tarafında ajan **asla mail göndermez**. Sadece cevap taslağı üretir, gönderen insandır.

### 5.6 Öğrenen kısım: onaylı sorgu kütüphanesi

Kullanıcı bir cevaba 👍 verirse soru ve SQL `verified_queries` tablosuna eklenir. `bilgi_ara` sonraki benzer
sorularda bunları "ekibin onayladığı sorgular, öncelikle bunları örnek al" diye modele gösterir. Bu yaklaşım
Snowflake Cortex Analyst'teki "verified queries" ve Databricks Genie'deki "trusted assets" fikrinin karşılığıdır.
Fine-tuning yerine seçildi, çünkü anında etki ediyor, şeffaf ve geri alınabilir.

### 5.7 Arayüz (Streamlit)

- **Soru sekmesi:** Sohbet, canlı adım görüntüleme, onay kutusu (Onayla / Düzeltmeyi gönder), sonuç tablosu,
  Excel indirme (soru ve SQL ayrı sayfada), 👍/👎 geri bildirim. Takip soruları için önceki soru ve cevap bağlam
  olarak eklenir.
- **Mail talebi sekmesi:** Mail metni yapıştırılır. İçinde SQL varsa değiştirilmeden çalıştırılır, yoksa normal soru
  gibi çözülür. Resmi bir cevap taslağı üretilir.
- **Ölçüm sekmesi:** Aşağıya bakınız.
- Spyder'dan `baslat.py` ile alt süreç olarak açılır (dosya izleyici kapalı, PID dosyası tutulur), `durdur.py` ile
  kapatılır.

---

## 6. Ölçüm (değerlendirme)

**Amaç:** Bir değişikliğin (yeni kartlar, prompt değişikliği, yeni araç) ajanı iyileştirip iyileştirmediğini sayıyla
görmek. Hissiyatla karar vermemek.

**Nasıl çalışır:**
1. Ekip, cevabını kesin bildiği gerçek soruları ve her birinin **doğru SQL'ini** bir Excel'e yazar (isteğe bağlı
   kategori ile).
2. Ajan her soruyu onay sormadan (`auto_approve`) çözer.
3. Doğru SQL de çalıştırılır ve iki **sonuç** karşılaştırılır. SQL metni değil sonuç karşılaştırılır (execution
   accuracy, Spider/BIRD standardı):
   - **DOĞRU:** Satırlar aynı. Satır ve kolon sırası ile kolon adları önemsiz, fazladan kolon serbest.
     Kolon eşlemesi değer kümesi üzerinden yapılır.
   - **İNCELE:** Birebir değil ama hücre bazlı örtüşme F1 ≥ 0.9 (BIRD'ün soft-F1 fikri). İnsan bakmalı.
   - **YANLIŞ / HATA:** Farklı sonuç ya da ajan sonuç üretemedi.
4. Ek metrikler: **tablo isabeti** (doğru SQL'deki gerçek tabloların kaçını ajan da kullandı), eksik/fazla satır,
   adım sayısı, süre.
5. Her çalıştırma `eval_runs` tablosuna kaydedilir. Geçmiş çalıştırmalar karşılaştırılarak gidişat izlenir
   (Databricks Genie'nin benchmark yaklaşımına benzer).

---

## 7. Bilinçli olarak yapılmayanlar ve nedenleri

| Yapılmayan | Neden |
|---|---|
| Fine-tuning | Elde etiketli soru-SQL verisi yok, bankanın modelini eğitme yetkisi yok. Onaylı sorgu kütüphanesi + iyi kartlar daha hızlı ve şeffaf. |
| INSERT/UPDATE/DELETE/DROP, başka şemaya yazma | Risk. Tek yazma işlemi kontrollü `tablo_olustur` (kendi şemada, yeni tablo, insan onayıyla). |
| Otomatik mail gönderimi | Yanlış veri dışarı gitmesin. Sadece taslak, insan gönderir. |
| Vektör veritabanı | Önce anahtar kelime aramasının yetersiz kaldığı yer ölçülecek. |
| Çoklu ajan (planlayıcı, SQL yazarı, denetçi ayrı modeller) | Paralel limit 1, 27B model. Tek ajan + kontrol araçları daha öngörülebilir. |

---

## 8. Bilinen zayıflıklar ve açık sorular (analiz için)

1. **Model gücü:** 27B model uzun, çok tablolu sorularda planı unutabilir veya kart dışı kolon uydurabilir. Plan onayı ve
   kart zorunluluğu bunu azaltıyor ama ölçülmedi.
2. **4096 çıktı sınırı:** Düşünme modu açıkken uzun bir adımdan sonra araç çağrısı yarım kalabiliyor. Yeniden deneme
   mantığı var. Düşünmeyi tamamen kapatmanın doğruluğa etkisi ölçülmedi.
3. **Tablo çözümleme oranı %54:** Referansların yarısı kesin bir tabloya bağlanıyor. Kalan belirsizlik kartları ve
   join kanıtlarını zayıflatıyor.
4. **Arama sadece kelimesel:** Ekibin farklı terimler kullandığı kavramlarda (ör. "batık" için `LIQ`, `TKP`) kaçırma
   olabilir. Embedding eklemek açık soru.
5. **Kart kalitesi:** İlk tam gece çalıştırması henüz yapılmadı. Ekip birkaç kartı gözden geçirip onaylı bilgileri
   artıracak.
6. **Ölçüm seti yok:** Ölçüm altyapısı hazır ama ekip henüz soru ve doğru SQL listesi oluşturmadı.
7. **Mail otomasyonu:** Outlook klasörünü (pywin32) izleyip taslak hazırlama ve geçmiş maillerden soru-SQL çiftleri
   çıkarma planlandı, yapılmadı.
8. **PD modeli modu:** Tekrarlayan iş akışlarını "tarif" (recipe) olarak tanımlama ve PD geliştirme adımları
   (veri seti oluşturma, WoE/IV, ayrıştırma gücü, kalibrasyon) planlandı, yapılmadı.

---

## 9. Yararlanılan kaynaklar ve benzer sistemler

- Benchmark'lar: **Spider 2.0** (kurumsal ölçekli text-to-SQL), **BIRD** (execution accuracy, soft-F1), DSBench.
- Endüstri: **Uber QueryGPT** (tablo seçimi ajanları, iş alanı bazlı bağlam), **LinkedIn SQL Bot** (bilgi grafiği +
  insan onayı), **Pinterest** text-to-SQL (tablo özetleri + arama), **Snowflake Cortex Analyst** (verified queries),
  **Databricks Genie** (trusted assets, benchmark tabanlı değerlendirme).
- Yöntem: **GraphRAG** (bilgiyi graph olarak düzenleme), **CHESS / LinkAlign** (şema bağlama ve tablo seçimi),
  **TEMPLAR** (geçmiş sorgu loglarından join/şablon öğrenme), LLM ile metadata üretimi (arXiv 2503.09003),
  Arctic-Text2SQL-R1.
- Düzenleyici: **EBA IRB / makine öğrenmesi** yönlendirmeleri (PD modeli tarafı için).
