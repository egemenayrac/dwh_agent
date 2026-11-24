# -*- coding: utf-8 -*-
"""
inceleme.py - Şirket inceleme ajanı (EWS dashboard'undan istek üzerine çağrılır)

Tek bir şirket için kaynakları toplar ve kaynak gösterilmiş kısa bir kredi izleme notu yazar. Ajan seviye ya da
skor VERMEZ (stage EWS kurallarıyla belirlenir); kanıtı toplar, çelişkileri ve eksik bilgiyi gösterir.

Kaynaklar (araçlar):
  ews_ozet, ews_haberler, ortaklar  : EWS çıktıları (llm_dogrulama.xlsx, portfolio_config.py; EWS_DIR klasöründe)
  ilan_ara                          : ilan.gov.tr (tüm ilan türleri; şirket adı ya da VKN ile)
  google_haber                      : Google News RSS (başlık, kaynak, tarih, bağlantı)
  kap_ara                           : KAP bildirimleri (kap_kaynak.py; kap_ingestion.py ile aynı mekanizma)
  dwh_sorgu                         : Oracle, yalnızca ayarlar.INCELEME_SORGULARI'ndaki şablonlar (:cid bind)

Kurallar:
  - Çekmek serbest, saklamak yasak: dış kaynak sonuçları diske yazılmaz, yalnızca bu oturumun notunda kullanılır.
  - Dış kaynaklara yalnızca kamuya açık kimlik gider (unvan, ortak adı, VKN). TCKN / bakiye / iç bilgi gitmez.
  - Her araç sonucundaki kayıt bir kaynak numarası taşır ([K1], [K2] ...); not yalnızca bunlara atıf yapabilir.
    not_denetimi() uydurma numarayı ve kaynakta geçmeyen alıntıyı işaretler.

Kullanım (Streamlit ya da test):
    sess, ctx = yeni_oturum(db, conn, sirket=88183343)
    durum = incele(sess, ctx, on_step=...)        # 'done' | 'waiting' | 'error'
    ctx.denetim                                   # not denetiminin bulguları (boş liste = temiz)
"""
import ast
import datetime as dt
import html
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import pandas as pd
import requests

import ajan_core as core
import kap_kaynak
import kb_builder as kb

# =============================== AYARLAR ===============================
# Kuruma özel değerler önce aynı klasördeki ayarlar_inceleme.py'den (yoksa ayarlar.py'den) okunur.
# ayarlar_inceleme.py GitHub'a gitmez (.gitignore); ayarlar.py'yi elle düzenlemeye gerek bırakmaz.
try:
    import ayarlar_inceleme as _ai
except ImportError:
    _ai = None


def _ayar(ad, varsayilan):
    return getattr(_ai, ad, getattr(kb, ad, varsayilan))


EWS_DIR = _ayar("EWS_DIR", "")                    # EWS çıktılarının klasörü (llm_dogrulama.xlsx ...)
EWS_EXCEL = _ayar("EWS_EXCEL", "llm_dogrulama.xlsx")
EWS_CONFIG = _ayar("EWS_CONFIG", "portfolio_config.py")
DIS_PROXY = _ayar("DIS_PROXY", "")                # dış siteler için proxy (http://host:port); boşsa doğrudan
HABER_GUN = 180            # ews_haberler varsayılan penceresi
HABER_MAKS = 25            # ews_haberler en fazla kayıt
ILAN_GUN = 365
ILAN_MAKS_SAYFA = 3        # ilan.gov.tr sayfa başına 20 ilan döndürüyor
ILAN_DETAY_MAKS = 6        # metni okunacak en fazla ilan
KAP_GUN = _ayar("KAP_GUN", 30)          # gün gün, 4 sn aralık: 30 gün ~2 dk
KAP_DETAY = _ayar("KAP_DETAY", 15)      # metni okunacak en fazla bildirim (önce ODA, sonra FR)
# DWH sorgu şablonları: {"ad": {"aciklama": "...", "sql": "SELECT ... WHERE ... = :cid"}}. Kuruma özeldir; repo'da
# DEĞİL, ayarlar.py'de tanımlanır. :cid, incelenen şirketin müşteri numarasıyla bind edilir (SQL'e yazılmaz).
INCELEME_SORGULARI = _ayar("INCELEME_SORGULARI", {}) or {}
DWH_MAKS_SATIR = 60
GOOGLE_GUN = 90
GOOGLE_MAKS = 25
DIS_BEKLEME = 1.0          # dış sitelere art arda istekler arasında saniye
# ======================================================================

ILGILI = ("odak", "ikincil")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
ILAN_BASE = "https://www.ilan.gov.tr"
ILAN_SEARCH = f"{ILAN_BASE}/api/api/services/app/Ad/AdsByFilter"
ILAN_DETAIL = f"{ILAN_BASE}/api/api/services/app/AdDetail/GetAdDetail"
ILAN_HEADERS = {"accept": "text/plain, application/json", "content-type": "application/json-patch+json",
                "origin": ILAN_BASE, "referer": f"{ILAN_BASE}/ilan/tum-ilanlar",
                "x-request-origin": "IGT-UI", "x-requested-with": "XMLHttpRequest"}
TCKN_RE = re.compile(r"(?<!\d)\d{11}(?!\d)")           # 11 haneli sayılar (TCKN, telefon) maskelenir
HUKUKI_EK_RE = re.compile(r"\b(A\.?\s?Ş\.?|ANONİM\s+ŞİRKETİ|ANONİM\s+ORTAKLIĞI|LTD\.?\s*ŞTİ\.?|LİMİTED\s+ŞİRKETİ|"
                          r"T\.?\s?A\.?\s?Ş\.?|A\.?\s?O\.?)\s*$", re.I)


def maskele(text):
    return TCKN_RE.sub("***", text or "")


def fold(text):
    return core.tr_fold(text)


def _tarih(v):
    t = pd.to_datetime(v, errors="coerce", dayfirst=isinstance(v, str) and bool(re.match(r"^\d{1,2}[./]", v)))
    return None if pd.isna(t) else t.tz_localize(None) if getattr(t, "tzinfo", None) else t


def _gun(v):
    t = _tarih(v)
    return t.strftime("%Y-%m-%d") if t is not None else str(v or "")[:10]


# ------------------------------- EWS verisi -------------------------------
class EwsVerisi:
    """EWS çıktılarını bir kez okur (salt okuma)."""

    def __init__(self, ews_dir):
        self.dir = ews_dir
        self._sheets, self._companies = {}, None

    def sheet(self, name):
        if name not in self._sheets:
            path = os.path.join(self.dir, EWS_EXCEL)
            if not os.path.exists(path):
                raise FileNotFoundError(f"EWS çıktısı yok: {path} (ayarlar.py'de EWS_DIR'i kontrol et)")
            self._sheets[name] = pd.read_excel(path, sheet_name=name)
        return self._sheets[name]

    def companies(self):
        if self._companies is None:
            path = os.path.join(self.dir, EWS_CONFIG)
            self._companies = {}
            if os.path.exists(path):
                src = open(path, encoding="utf-8").read()
                m = re.search(r"COMPANIES\s*=\s*", src)
                if m:
                    body = src[m.end():]
                    end = body.find("\n# fmt: on")
                    self._companies = ast.literal_eval(body[:end] if end != -1 else body)
        return self._companies


def _sirket_satiri(df, sirket):
    """sirket: counterparty_id (sayı/str) ya da unvan parçası. Eşleşen satırları döner."""
    s = str(sirket).strip()
    ids = pd.to_numeric(df["Şirket"], errors="coerce")
    if s.isdigit():
        return df[ids == int(s)]
    f = fold(s)
    return df[df["Şirket_Adi"].astype(str).map(fold).str.contains(re.escape(f), na=False)]


def cekirdek_ad(unvan, aliases=()):
    """Dış arama için şirketin kısa adı: 2-4 kelimelik en uzun alias, yoksa hukuki eki atılmış unvan."""
    adaylar = [a for a in aliases if 2 <= len(str(a).split()) <= 4 and not HUKUKI_EK_RE.search(str(a))]
    if adaylar:
        return max(adaylar, key=len)
    ad = str(unvan or "").strip()
    for _ in range(3):
        ad = HUKUKI_EK_RE.sub("", ad).strip(" .,")
    return ad


def dis_oturum(kullanici="", sifre=""):
    """Dış siteler (ilan.gov.tr, Google News, KAP) için HTTP oturumu. DIS_PROXY tanımlıysa proxy üzerinden;
    kullanıcı/şifre arayüzden gelir, dosyaya yazılmaz."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"})
    if DIS_PROXY:
        proxy = DIS_PROXY
        if kullanici:
            u, p = urllib.parse.quote(kullanici, safe=""), urllib.parse.quote(sifre or "", safe="")
            proxy = proxy.replace("http://", f"http://{u}:{p}@", 1)
        s.proxies.update({"http": proxy, "https": proxy})
        s.verify = False   # kurumsal SSL incelemesi (mevcut KAP/haber kodlarındaki uygulama)
    return s


# ------------------------------- Bağlam ve araçlar -------------------------------
class IncelemeCtx(core.Ctx):
    """İnceleme ajanının araçları. Veri ambarı ajanının Ctx'ini genişletir (Oracle bağlantısı ve db aynı)."""

    def __init__(self, db, conn=None, sirket=None, ews_dir=None, http=None):
        super().__init__(db, conn)
        self.sirket = sirket
        self.ews = EwsVerisi(ews_dir if ews_dir is not None else EWS_DIR)
        self.kaynaklar = {}          # "K1" -> {tur, baslik, tarih, url, metin}
        self.denetim = []
        self.http = http or dis_oturum()
        self._son_istek = 0.0

    def _bekle(self):
        gecen = time.monotonic() - self._son_istek
        if gecen < DIS_BEKLEME:
            time.sleep(DIS_BEKLEME - gecen)
        self._son_istek = time.monotonic()

    def kaynak(self, tur, baslik, tarih="", url="", metin=""):
        kid = f"K{len(self.kaynaklar) + 1}"
        self.kaynaklar[kid] = {"tur": tur, "baslik": str(baslik or "")[:300], "tarih": str(tarih or "")[:10],
                               "url": str(url or ""), "metin": str(metin or "")}
        return kid

    def _yok(self, tur, mesaj):
        """Sonuç yok / erişilemedi bulgusu da bir kaynak numarası alır; not 'yok' bulgusuna da atıf yapabilir."""
        k = self.kaynak(tur, mesaj, dt.date.today().isoformat(), "", mesaj)
        return f"[{k}] {mesaj}."

    def _bul(self, sirket):
        sirket = sirket if sirket not in (None, "") else self.sirket
        df = self.ews.sheet("Birlesik")
        rows = _sirket_satiri(df, sirket)
        if rows.empty:
            raise ValueError(f"'{sirket}' EWS portföyünde bulunamadı. Numara (counterparty_id) ya da unvan dene.")
        if len(rows) > 1:
            adlar = "; ".join(f"{r['Şirket']} {r['Şirket_Adi']}" for _, r in rows.head(8).iterrows())
            raise ValueError(f"Birden çok şirket eşleşti, numarayla tekrar çağır: {adlar}")
        return rows.iloc[0]

    # ---- EWS ----
    def ews_ozet(self, sirket=""):
        r = self._bul(sirket)
        sid = int(r["Şirket"])
        alan = ["Birlesik_Seviye", "Birlesik_Skor", "Birlesik_Kural", "Risk_Matrisi", "Haber_Sinyali", "Haber_Skoru",
                "Finansal_Seviye", "Finansal_Skor", "Finansal_Veri_Kapsami", "Finansal_Tarih", "DSCR_Degeri",
                "Toplam_Bakiye_TRY", "Finansal_Puan_Detay", "Not"]
        ozet = "; ".join(f"{a}={r[a]}" for a in alan if a in r.index and pd.notna(r[a]) and str(r[a]) != "")
        k = self.kaynak("EWS", f"EWS Birlesik satırı: {r['Şirket_Adi']}", metin=ozet)
        out = [f"[{k}] {r['Şirket_Adi']} (no {sid}) — EWS özeti: {ozet}"]
        try:
            defter = self.ews.sheet("Olay_Defteri")
            olaylar = defter[pd.to_numeric(defter["Şirket"], errors="coerce") == sid]
        except (ValueError, KeyError):
            olaylar = pd.DataFrame()
        if olaylar.empty:
            out.append("Olay defterinde kayıt yok.")
        for _, o in olaylar.iterrows():
            metin = " | ".join(str(o.get(c, "")) for c in ("Tanim", "Zaman_Cizelgesi", "Kapanis_Alinti") if pd.notna(o.get(c)))
            k = self.kaynak("EWS-olay", f"{o.get('Olay')} {o.get('Olay_Tipi')} ({o.get('Seviye')}, {o.get('Durum')})",
                            _gun(o.get("Son_Haber") or o.get("Acilis")), o.get("Ilk_Haber_URL") or "", metin)
            out.append(f"[{k}] Olay {o.get('Olay')}: {o.get('Olay_Tipi')} · {o.get('Seviye')} · {o.get('Durum')} · "
                       f"açılış {_gun(o.get('Acilis'))} · {o.get('Haber_Sayisi')} haber · etki: {o.get('Sinyale_Etkisi')} "
                       f"· {core.short(o.get('Tanim'), 300)}")
        return "\n".join(out)

    def ews_haberler(self, sirket="", gun=HABER_GUN, sadece_ilgili=True):
        r = self._bul(sirket)
        sid = int(r["Şirket"])
        kay = self.ews.sheet("Kayıtlar")
        kay = kay[pd.to_numeric(kay["Şirket"], errors="coerce") == sid].copy()
        if sadece_ilgili and "LLM_Alaka" in kay:
            kay = kay[kay["LLM_Alaka"].isin(ILGILI)]
        kay["_t"] = kay["Tarih"].map(_tarih)
        sinir = pd.Timestamp.now() - pd.Timedelta(days=int(gun))
        kay = kay[kay["_t"].isna() | (kay["_t"] >= sinir)].sort_values("_t", ascending=False)
        if kay.empty:
            return self._yok("EWS", f"{r['Şirket_Adi']} için son {gun} günde {'ilgili ' if sadece_ilgili else ''}"
                                    f"EWS haberi yok")
        out = [f"{r['Şirket_Adi']}: son {gun} günde {len(kay)} haber (en yeni {min(len(kay), HABER_MAKS)} tanesi):"]
        for _, h in kay.head(HABER_MAKS).iterrows():
            alinti = h.get("LLM_Alinti") if pd.notna(h.get("LLM_Alinti")) else ""
            metin = " ".join(str(h.get(c)) for c in ("Başlık", "LLM_Ozet", "LLM_Alinti", "Metin_Snippet") if pd.notna(h.get(c)))
            k = self.kaynak("haber", h.get("Başlık"), _gun(h.get("Tarih")), h.get("URL") or "", metin)
            ortak = f" · ortak üzerinden: {h.get('Ortak')}" if pd.notna(h.get("Ortak")) and str(h.get("Ortak")) else ""
            out.append(f"[{k}] {_gun(h.get('Tarih'))} · {h.get('Kaynak')} · {h.get('Başlık')} · olay: {h.get('LLM_Olay')} "
                       f"({h.get('LLM_Durum')}){ortak}" + (f" · alıntı: \"{core.short(alinti, 250)}\"" if alinti else ""))
        return "\n".join(out)

    def ortaklar(self, sirket=""):
        r = self._bul(sirket)
        comp = self.ews.companies().get(int(r["Şirket"])) or {}
        p = comp.get("partners") or []
        if not p:
            return f"{r['Şirket_Adi']}: portföy config'inde %50'den büyük hakim ortak kaydı yok."
        return f"{r['Şirket_Adi']} hakim ortakları: " + "; ".join(f"{x.get('name')} (%{x.get('share')})" for x in p)

    def _arama_adi(self, sirket, sorgu):
        if sorgu:
            return str(sorgu).strip()
        r = self._bul(sirket)
        comp = self.ews.companies().get(int(r["Şirket"])) or {}
        return cekirdek_ad(r["Şirket_Adi"], comp.get("aliases") or ())

    # ---- ilan.gov.tr ----
    def ilan_ara(self, sirket="", sorgu="", vkn="", gun=ILAN_GUN):
        ad = self._arama_adi(sirket, sorgu)
        terimler = [t for t in (str(vkn).strip(), ad) if t]
        ppdmin = (dt.date.today() - dt.timedelta(days=int(gun))).strftime("%d.%m.%Y")
        ilanlar, goruldu, toplam = [], set(), {}
        for terim in terimler:
            for sayfa in range(ILAN_MAKS_SAYFA):
                self._bekle()
                body = {"keys": {"q": [terim], "ppdmin": [ppdmin]}, "skipCount": sayfa * 20, "maxResultCount": 20}
                if sayfa:
                    body["keys"]["currentPage"] = [sayfa + 1]
                res = self.http.post(ILAN_SEARCH, json=body, headers=ILAN_HEADERS, timeout=25)
                if res.status_code != 200:
                    return f"ilan.gov.tr HTTP {res.status_code} döndü ('{terim}'). Erişim/proxy kontrol edilmeli."
                sonuc = res.json().get("result") or {}
                toplam[terim] = sonuc.get("numFound")
                ads = sonuc.get("ads") or []
                for a in ads:
                    if str(a.get("id")) not in goruldu:
                        goruldu.add(str(a.get("id")))
                        ilanlar.append(a)
                if len(ads) < 20:
                    break
        if not ilanlar:
            return self._yok("ilan", f"ilan.gov.tr: '{' / '.join(terimler)}' için son {gun} günde ilan yok")
        out = [f"ilan.gov.tr: '{' / '.join(terimler)}' için {len(ilanlar)} ilan (son {gun} gün; bulunan: {toplam}). "
               f"Ad benzerliği nedeniyle başka şirketlere ait ilanlar olabilir; unvanı kontrol et."]
        for i, a in enumerate(ilanlar):
            kategori = ", ".join(c.get("name", "") for c in (a.get("categories") or []) if isinstance(c, dict))
            metin = ""
            if i < ILAN_DETAY_MAKS:
                self._bekle()
                d = self.http.get(ILAN_DETAIL, params={"id": a.get("id")}, headers=ILAN_HEADERS, timeout=20)
                if d.status_code == 200:
                    icerik = str((d.json().get("result") or {}).get("content") or "")
                    metin = maskele(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", icerik))).strip())
            url = ILAN_BASE + str(a.get("urlStr") or f"/ilan/{a.get('id')}")
            k = self.kaynak("ilan", a.get("title"), _gun(a.get("publishStartDate")), url, f"{a.get('title')} {metin}")
            out.append(f"[{k}] {_gun(a.get('publishStartDate'))} · {kategori or '-'} · {a.get('advertiserName') or ''} · "
                       f"{a.get('title')}" + (f" · metin: {core.short(metin, 500)}" if metin else ""))
        return "\n".join(out)

    # ---- DWH (yalnızca onaylı şablonlar) ----
    def dwh_sorgu(self, ad="", sirket=""):
        if not INCELEME_SORGULARI:
            return self._yok("DWH", "DWH sorgu şablonu tanımlı değil (ayarlar.py: INCELEME_SORGULARI); banka verisi "
                                    "incelenmedi")
        if ad not in INCELEME_SORGULARI:
            return "Şablonlar: " + "; ".join(f"{k}: {v.get('aciklama', '')}" for k, v in INCELEME_SORGULARI.items())
        if self.conn is None:
            return "Oracle bağlantısı yok (sol panelden bağlanılmalı); banka verisi alınamadı."
        cid = int(self._bul(sirket)["Şirket"])
        t = INCELEME_SORGULARI[ad]
        sql = core.check_select(t["sql"])
        with self.conn.cursor() as cur:
            cur.execute(sql, {"cid": cid})
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(DWH_MAKS_SATIR + 1)
        df = pd.DataFrame(rows[:DWH_MAKS_SATIR], columns=cols)
        if df.empty:
            return self._yok("DWH", f"DWH '{ad}': kayıt yok")
        tablo = df.to_string(index=False, max_colwidth=40)
        k = self.kaynak("DWH", f"{ad}: {t.get('aciklama', '')}", dt.date.today().isoformat(), "", tablo)
        ek = f" (ilk {DWH_MAKS_SATIR} satır)" if len(rows) > DWH_MAKS_SATIR else ""
        return f"[{k}] DWH '{ad}' — {t.get('aciklama', '')}{ek}:\n{tablo}"

    # ---- KAP ----
    def _kap_istemci(self):
        if getattr(self, "_kap", None) is None:
            s = requests.Session()               # KAP'ın kendi oturumu; proxy/SSL ayarı dış oturumla aynı
            s.proxies.update(getattr(self.http, "proxies", {}) or {})
            s.verify = getattr(self.http, "verify", True)
            self._kap = kap_kaynak.KapClient(s)
        return self._kap

    def kap_ara(self, sirket="", sorgu="", gun=KAP_GUN):
        r = self._bul(sirket)
        comp = self.ews.companies().get(int(r["Şirket"])) or {}
        ad = str(sorgu).strip() or cekirdek_ad(r["Şirket_Adi"], comp.get("aliases") or ())
        kodlar = [str(t).upper() for t in (comp.get("tickers") or []) if t]
        kap = self._kap_istemci()
        adaylar, durum = kap.sirket_ara(ad)
        if durum != "ok":
            return f"KAP şirket araması başarısız ({durum}). Erişim/proxy kontrol edilmeli."
        fa = fold(ad)
        secilen = next((a for a in adaylar if fold(a["ad"]).startswith(fa)), None) or \
            next((a for a in adaylar if fa in fold(a["ad"])), None)
        if secilen is None and not kodlar:
            liste = "; ".join(a["ad"] for a in adaylar[:5]) or "yok"
            return (f"KAP'ta '{ad}' adlı işlem gören şirket bulunamadı (halka açık olmayabilir). Adaylar: {liste}. "
                    f"Doğru şirket adaylardaysa sorgu parametresiyle tam adını ver.")
        h = kap_kaynak.hedef(kodlar + ([secilen["kod"]] if secilen and secilen["kod"] else []),
                             secilen["oid"] if secilen else "", secilen["ad"] if secilen else "")
        kayitlar, ozet = kap.bildirimler(h, gun=int(gun), detay_maks=KAP_DETAY)
        kim = f"{secilen['ad']} ({secilen['kod']})" if secilen else ", ".join(kodlar)
        if not kayitlar:
            return self._yok("KAP", f"KAP: {kim} için son {gun} günde bildirim yok ({ozet})")
        out = [f"KAP: {kim} — son {gun} günde {len(kayitlar)} bildirim ({ozet}). Metni okunanlar en fazla {KAP_DETAY}."]
        for b in kayitlar:
            tarih = b["published_at"].strftime("%Y-%m-%d") if b["published_at"] else ""
            metin = maskele(b.get("body_text") or b.get("summary_text") or "")
            baslik = b.get("subject") or b.get("summary_text") or "KAP bildirimi"
            k = self.kaynak("KAP", baslik, tarih, b.get("disclosure_link"), f"{baslik} {b.get('summary_text') or ''} {metin}")
            out.append(f"[{k}] {tarih} · {b.get('class_label') or b.get('disclosure_class') or '-'} · {baslik}"
                       + (f" · özet: {core.short(b.get('summary_text'), 200)}" if b.get("summary_text") else "")
                       + (f" · metin: {core.short(metin, 600)}" if metin and metin != b.get("summary_text") else ""))
        return "\n".join(out)

    # ---- Google News ----
    def google_haber(self, sirket="", sorgu="", gun=GOOGLE_GUN):
        ad = self._arama_adi(sirket, sorgu)
        q = f"\"{ad}\" when:{int(gun)}d"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "tr", "gl": "TR", "ceid": "TR:tr"})
        self._bekle()
        res = self.http.get(url, timeout=25)
        if res.status_code != 200:
            return f"Google News HTTP {res.status_code} döndü. Erişim/proxy kontrol edilmeli."
        items = ET.fromstring(res.content).findall("./channel/item")
        if not items:
            return self._yok("google", f"Google News: '{ad}' için son {gun} günde haber yok")
        out = [f"Google News: '{ad}' için {len(items)} haber (en yeni {min(len(items), GOOGLE_MAKS)}; yalnızca başlık). "
               f"Spor kulübü, sponsorluk ve reklam haberleri gürültüdür."]
        for it in items[:GOOGLE_MAKS]:
            baslik = html.unescape(it.findtext("title") or "")
            tarih = _gun(it.findtext("pubDate"))
            kaynak = it.findtext("source") or ""
            k = self.kaynak("google", baslik, tarih, it.findtext("link") or "", baslik)
            out.append(f"[{k}] {tarih} · {kaynak} · {baslik}")
        return "\n".join(out)


# ------------------------------- Araç tanımları -------------------------------
S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_SIRKET = {"sirket": {"type": "string", "description": "counterparty_id ya da unvan; boşsa incelenen şirket"}}
INCELEME_TOOLS = [
    core._fn("ews_ozet", "EWS özeti: birleşik/haber/finansal seviye ve skor, risk matrisi, olay defterindeki olaylar. "
             "İlk adım budur.", dict(_SIRKET), []),
    core._fn("ews_haberler", "EWS haber havuzunda şirketle eşleşmiş haberler (tarih, kaynak, başlık, olay tipi, alıntı).",
             dict(_SIRKET, gun=I, sadece_ilgili=B), []),
    core._fn("ortaklar", "Şirketin %50'den büyük hakim ortakları (portföy config'i).", dict(_SIRKET), []),
    core._fn("ilan_ara", "ilan.gov.tr'de şirketin ilanları (tebligat: konkordato/iflas; icra satışları; ihaleler). "
             "sorgu boşsa şirketin kısa adı kullanılır; ortak için ortağın adını sorgu olarak ver.",
             dict(_SIRKET, sorgu=S, vkn=S, gun=I), []),
    core._fn("dwh_sorgu", "Banka verisi (bakiye, finansallar, sınıflama notu...) için ONAYLI sorgu şablonunu çalıştırır. "
             "ad boşsa şablon listesini döner. SQL yazamazsın; yalnızca şablon adı verirsin.",
             dict(_SIRKET, ad=S), []),
    core._fn("kap_ara", "KAP'ta şirketin bildirimleri (özel durum açıklamaları, finansal raporlar). Yalnızca borsada "
             "işlem gören şirketler; 30 gün ~2-3 dakika sürer. sorgu boşsa şirketin kısa adı.",
             dict(_SIRKET, sorgu=S, gun=I), []),
    core._fn("google_haber", "Google News'te son haber başlıkları (EWS havuzunda olmayanlar için). sorgu boşsa şirketin "
             "kısa adı; ortak için ortağın adını ver.", dict(_SIRKET, sorgu=S, gun=I), []),
    next(t for t in core.TOOLS if t["function"]["name"] == "kullaniciya_sor"),
    core._fn("cevap_ver", "Not bitince çağrılır. 'cevap' alanına notun tamamı (şablondaki bölümlerle).",
             {"cevap": S, "varsayimlar": S}, ["cevap"]),
]

INCELEME_SYSTEM = """Sen bir bankanın kredi riski izleme ekibinde çalışan bir analistsin. Görevin TEK bir şirket için
kaynakları toplayıp kısa bir kredi izleme notu yazmak. Bugünün tarihi: {today}.

ÇALIŞMA YÖNTEMİ
1. ews_ozet ile başla. Sonra ews_haberler, ortaklar, ilan_ara, kap_ara ve google_haber'i çağır. dwh_sorgu'yu
   ad vermeden çağırıp şablonları gör, her şablonu bir kez çalıştır (banka verisi rakamları yalnızca buradan gelir). Hakim ortak varsa ilan_ara ve
   google_haber'i ortağın adıyla da çağır.
2. Araç sonuçlarındaki her kayıt bir kaynak numarası taşır ([K1], [K2] ...). Nottaki HER bilgi cümlesinin sonuna
   dayandığı kaynak numarasını yaz. Araç çıktısında olmayan bir bilgiyi, rakamı ya da kaynağı YAZMA; kendi bilgini ekleme.
3. Alıntı yapacaksan kaynaktaki ifadeyi kelimesi kelimesine ve tırnak içinde ver.
4. Seviye, stage ya da skor VERME ve değiştirme; EWS'in seviyesini olduğu gibi aktar. Görevin kanıtı özetlemek.
5. Ad benzerliğine dikkat: ilan ve Google sonuçlarında başka bir şirkete, spor kulübüne, sponsorluğa, reklama ya da
   kampanyaya ait kayıtları notta kullanma (ör. "Türk Hava Yolları 0-3 Fenerbahçe" bir voleybol maçıdır).
6. Bir araç hata verirse ya da sonuç yoksa bunu 5. bölümde, aracın verdiği kaynak numarasıyla yaz; tahmin yürütme.
7. Bitince cevap_ver'i çağır. 'cevap' alanına notun tamamını yaz (en fazla ~500 kelime).

NOT ŞABLONU
<Şirket unvanı> — Kredi İzleme Notu ({today}, ajan taslağı)
1. Özet (3-5 madde)
2. Olaylar ve haberler
3. Resmi kayıtlar (ilan.gov.tr, KAP)
4. Banka verisi ve EWS seviyesi (dwh_sorgu ve EWS özetinden; rakamları kaynaktaki gibi yaz)
5. Çelişkiler ve eksik bilgi
6. Analiste sorular / önerilen kontroller
Kaynaklar: her [K#] için tek satır (tür, tarih, başlık)"""


# ------------------------------- Not denetimi -------------------------------
_ALINTI_RE = re.compile(r"[\"“«]([^\"”»]{15,400})[\"”»]")
_MADDE_RE = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+(.*)$")
_KAYNAKSIZ_SERBEST = ("eksik", "çelişki", "soru", "kontrol", "kaynak")   # bu başlıkların altında kaynak şart değil


def not_denetimi(metin, kaynaklar):
    """Notu kaynaklara karşı denetler; bulgu listesi döner (boş = temiz)."""
    bulgular = []
    atif = set(re.findall(r"\[(K\d+)\]", metin or ""))
    for k in sorted(atif - set(kaynaklar), key=lambda x: int(x[1:])):
        bulgular.append(f"{k} diye bir kaynak yok (uydurma kaynak numarası).")
    havuz = fold(" ".join(f"{v['baslik']} {v['metin']}" for v in kaynaklar.values()))
    for a in _ALINTI_RE.findall(metin or ""):
        if fold(a).strip() and fold(a).strip() not in havuz:
            bulgular.append(f"Alıntı kaynaklarda birebir geçmiyor: \"{core.short(a, 120)}\"")
    bolum = ""
    for satir in (metin or "").splitlines():
        s = satir.strip()
        if not s:
            continue
        m = _MADDE_RE.match(s)
        baslik_mi = bool(re.match(r"^\d+\.\s+\S", s)) and len(s) < 80 and "[K" not in s
        if baslik_mi or s.lower().startswith("kaynaklar"):
            bolum = fold(s)
            continue
        if any(w in bolum for w in _KAYNAKSIZ_SERBEST):
            continue
        if m and "[K" not in s and len(m.group(1)) > 25:
            bulgular.append(f"Kaynaksız madde: \"{core.short(m.group(1), 120)}\"")
    return bulgular


# ------------------------------- Oturum ve çalıştırma -------------------------------
def yeni_oturum(db, conn=None, sirket=None, ews_dir=None, http=None):
    ctx = IncelemeCtx(db, conn, sirket=sirket, ews_dir=ews_dir, http=http)
    system = INCELEME_SYSTEM.format(today=dt.date.today().isoformat())
    soru = f"İncelenecek şirket: {sirket}. Kaynakları topla ve notu yaz."
    sess = core.Session(soru, tools=INCELEME_TOOLS, system=system)
    return sess, ctx


def incele(sess, ctx, on_step=None, auto_approve=True):
    """Ajanı çalıştırır, notu denetler; bulgu varsa ajana bir kez düzeltme için geri gönderir."""
    durum = core.run(sess, ctx, on_step=on_step, auto_approve=auto_approve)
    if durum != "done":
        return durum
    ctx.denetim = not_denetimi((sess.result or {}).get("cevap", ""), ctx.kaynaklar)
    if ctx.denetim:
        ilk_sonuc, ilk_denetim = dict(sess.result or {}), list(ctx.denetim)
        sess.messages.append({"role": "user", "content": (
            "Not denetimi şu sorunları buldu:\n- " + "\n- ".join(ctx.denetim) +
            "\nBu sorunları düzelt (kaynaksız cümleleri ya kaynakla destekle ya da çıkar) ve cevap_ver'i tekrar çağır.")})
        durum = core.run(sess, ctx, on_step=on_step, auto_approve=auto_approve)
        yeni = (sess.result or {}).get("cevap", "")
        if durum == "done" and yeni.strip():
            ctx.denetim = not_denetimi(yeni, ctx.kaynaklar)
        else:   # düzeltme turu başarısız: ilk not, denetim uyarılarıyla birlikte geri gelir
            sess.result, ctx.denetim = ilk_sonuc, ilk_denetim + [f"Düzeltme turu tamamlanamadı ({sess.error or durum})."]
            sess.status, sess.error, durum = "done", None, "done"
    return durum


def not_markdown(sess, ctx):
    """İndirilecek / kaydedilecek not: ajanın notu + denetim uyarıları + kaynak listesi."""
    r = sess.result or {}
    satirlar = [r.get("cevap", "").strip(), ""]
    if ctx.denetim:
        satirlar += ["> **Denetim uyarıları** (kontrol edilmeli):"] + [f"> - {d}" for d in ctx.denetim] + [""]
    satirlar.append("## Kaynaklar (araç çıktıları)")
    for k, v in ctx.kaynaklar.items():
        bag = f" — {v['url']}" if v["url"] else ""
        satirlar.append(f"- [{k}] {v['tur']} · {v['tarih']} · {v['baslik']}{bag}")
    satirlar.append(f"\n_Ajan taslağıdır; seviye ve skor EWS kurallarındandır. {dt.datetime.now():%Y-%m-%d %H:%M}_")
    return "\n".join(satirlar)


def notu_kaydet(md, sirket, klasor=None):
    """Notu iç klasöre yazar (kb/incelemeler). Dış kaynak ham sonuçları yazılmaz; yalnızca not."""
    klasor = klasor or os.path.join(kb.KB_DIR, "incelemeler")
    os.makedirs(klasor, exist_ok=True)
    ad = re.sub(r"[^\w-]+", "_", str(sirket))[:40]
    yol = os.path.join(klasor, f"{ad}_{dt.datetime.now():%Y%m%d_%H%M}.md")
    with open(yol, "w", encoding="utf-8") as f:
        f.write(md)
    return yol


def kaynak_listesi(ctx):
    """Ekranda ve indirilen notta gösterilecek kaynak tablosu."""
    return pd.DataFrame([{"no": k, "tür": v["tur"], "tarih": v["tarih"], "başlık": v["baslik"], "bağlantı": v["url"]}
                         for k, v in ctx.kaynaklar.items()])
