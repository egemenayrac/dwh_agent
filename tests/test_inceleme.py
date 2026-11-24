# -*- coding: utf-8 -*-
"""inceleme.py testleri: Oracle, LLM ve internet olmadan (sahte EWS çıktısı, sahte HTTP, senaryolu sahte LLM)."""
import json
import re
import os
import sqlite3
import sys
import types

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:  # bankada kurulu; geliştirme makinesinde yoksa yalnızca içe aktarma için boş modül
    import oracledb  # noqa: F401
except ImportError:
    sys.modules["oracledb"] = types.SimpleNamespace(makedsn=lambda *a, **k: "", defaults=types.SimpleNamespace())

import ajan_core as core  # noqa: E402
import inceleme  # noqa: E402

SID = 4242


def _ews_dir(tmp_path):
    d = tmp_path / "ews"
    d.mkdir()
    birlesik = pd.DataFrame([
        {"Şirket": SID, "Şirket_Adi": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "Birlesik_Seviye": "Yakın İzleme (Stage 2)",
         "Birlesik_Skor": 52.0, "Risk_Matrisi": "Bilanço İzlemede × Yüksek Haber Sinyali", "Haber_Skoru": 45,
         "Finansal_Skor": 61.5, "Finansal_Tarih": "31/12/2025"},
        {"Şirket": 7, "Şirket_Adi": "BAŞKA A.Ş.", "Birlesik_Seviye": "Düşük Risk (Stage 1)", "Birlesik_Skor": 10.0},
    ])
    defter = pd.DataFrame([{"Şirket": SID, "Şirket_Adi": "ÖRNEK ÇELİK", "Olay": "O1", "Olay_Tipi": "dava_ceza",
                            "Seviye": "HIGH", "Durum": "açık", "Sinyale_Etkisi": "Stage 2", "Son_Haber": "2026-11-10",
                            "Acilis": "2026-11-01", "Haber_Sayisi": 2, "Tanim": "Rekabet Kurumu soruşturma açtı",
                            "Ilk_Haber_URL": "https://ornek.com/h1"}])
    kayit = pd.DataFrame([
        {"Tarih": "2026-11-10", "Kaynak": "Dünya", "Şirket": SID, "Şirket_Adi": "ÖRNEK ÇELİK", "Başlık": "Örnek Çelik'e soruşturma",
         "URL": "https://ornek.com/h1", "LLM_Alaka": "odak", "LLM_Olay": "dava_ceza", "LLM_Durum": "gerceklesti",
         "LLM_Alinti": "Rekabet Kurumu Örnek Çelik hakkında soruşturma açtı", "LLM_Ozet": "Soruşturma", "Ortak": None},
        {"Tarih": "2026-11-12", "Kaynak": "Ekonomim", "Şirket": SID, "Şirket_Adi": "ÖRNEK ÇELİK", "Başlık": "Alakasız",
         "URL": "https://ornek.com/h2", "LLM_Alaka": "alakasiz", "LLM_Olay": "", "LLM_Durum": "", "LLM_Alinti": ""},
    ])
    with pd.ExcelWriter(d / "llm_dogrulama.xlsx") as w:
        birlesik.to_excel(w, sheet_name="Birlesik", index=False)
        defter.to_excel(w, sheet_name="Olay_Defteri", index=False)
        kayit.to_excel(w, sheet_name="Kayıtlar", index=False)
    companies = {SID: {"name": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.",
                       "aliases": ["ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "ÖRNEK ÇELİK SANAYİ", "ÖRNEK ÇELİK", "ÖRNEK"],
                       "tickers": [], "partners": [{"name": "ÖRNEK HOLDİNG A.Ş.", "share": 62.0}]}}
    (d / "portfolio_config.py").write_text("COMPANIES = " + repr(companies) + "\n", encoding="utf-8")
    return str(d)


class FakeResp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        return self._body


class FakeHttp:
    """ilan.gov.tr ve Google News cevaplarını taklit eder; gönderilen istekleri kaydeder."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        return FakeResp(200, {"result": {"numFound": 1, "ads": [{
            "id": 99, "title": "Konkordato geçici mühlet kararının ilanen tebliği", "advertiserName": "X ASLİYE TİCARET",
            "publishStartDate": "20.11.2026", "urlStr": "/ilan/99/konkordato", "categories": [{"name": "Tebligat ve Duyurular"}]}]}})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if "news.google.com" in url:
            rss = ("<rss><channel>"
                   "<item><title>Örnek Çelik ihracatını artırdı - Dünya</title><link>https://news.google.com/a1</link>"
                   "<pubDate>Fri, 20 Nov 2026 08:00:00 GMT</pubDate><source>Dünya</source></item>"
                   "</channel></rss>").encode("utf-8")
            return FakeResp(200, content=rss)
        return FakeResp(200, {"result": {"content": "<p>ÖRNEK ÇELİK A.Ş. ve 12345678901 TC kimlik numaralı ortak hakkında "
                                                    "geçici mühlet kararı verildi.</p>"}})


def _ctx(tmp_path, http=None):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    core.init_db(db)
    inceleme.DIS_BEKLEME = 0
    return inceleme.IncelemeCtx(db, None, sirket=SID, ews_dir=_ews_dir(tmp_path), http=http or FakeHttp())


def test_session_default_tools_unchanged_and_custom_tools():
    varsayilan = core.Session("soru")
    assert varsayilan.tools is None and "veri analisti ajanısın" in varsayilan.messages[0]["content"]
    ozel = core.Session("soru", tools=inceleme.INCELEME_TOOLS, system="SİSTEM")
    assert ozel.tools is inceleme.INCELEME_TOOLS and ozel.messages[0]["content"] == "SİSTEM"


def test_ews_tools_register_sources(tmp_path):
    ctx = _ctx(tmp_path)
    ozet = ctx.ews_ozet()
    assert "[K1]" in ozet and "Yakın İzleme" in ozet and "[K2] Olay O1" in ozet
    haber = ctx.ews_haberler()
    assert "soruşturma" in haber and "Alakasız" not in haber          # yalnızca ilgili haberler
    assert ctx.kaynaklar["K3"]["url"] == "https://ornek.com/h1"
    assert "ÖRNEK HOLDİNG" in ctx.ortaklar()
    assert "Birden çok" in _hata(lambda: ctx.ews_ozet("A.Ş."))         # belirsiz ad -> numara iste


def _hata(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return ""


def test_external_tools_send_only_public_name_and_mask_tckn(tmp_path):
    http = FakeHttp()
    ctx = _ctx(tmp_path, http)
    ilan = ctx.ilan_ara()
    assert "Konkordato geçici mühlet" in ilan and "12345678901" not in ilan and "***" in ilan
    aranan = [c[2]["keys"]["q"] for c in http.calls if c[0] == "POST"]
    assert aranan == [["ÖRNEK ÇELİK SANAYİ"]]                          # kısa ad; numara/iç bilgi gitmez
    g = ctx.google_haber()
    assert "ihracatını artırdı" in g
    gurl = [c[1] for c in http.calls if "news.google.com" in c[1]][0]
    assert str(SID) not in gurl and "%C3%96RNEK" in gurl.upper()
    assert all(v["url"] for v in ctx.kaynaklar.values())


def test_not_denetimi():
    kaynaklar = {"K1": {"baslik": "Örnek Çelik'e soruşturma", "metin": "Rekabet Kurumu Örnek Çelik hakkında soruşturma açtı"}}
    temiz = ("1. Özet\n- Rekabet Kurumu soruşturma açtı ve dosya sürüyor [K1]\n"
             "5. Çelişkiler ve eksik bilgi\n- KAP aracı henüz yok, bu nedenle bildirimler incelenmedi\n"
             "Kaynaklar: [K1] haber")
    assert inceleme.not_denetimi(temiz, kaynaklar) == []
    kirli = ("1. Özet\n- Şirket konkordato başvurusu yaptı ve mühlet aldı [K9]\n"
             "- Şirketin satışları geçen yıla göre yüzde otuz düştü\n"
             "2. Olaylar\n- Haberde \"şirket iflasın eşiğinde olduğunu açıkladı\" deniyor [K1]")
    b = inceleme.not_denetimi(kirli, kaynaklar)
    assert any("K9" in x for x in b) and any("Kaynaksız madde" in x for x in b) and any("birebir" in x for x in b)


def _arac(name, args, i):
    return {"id": f"c{i}", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def test_incele_end_to_end_with_fake_llm_and_one_correction(tmp_path, monkeypatch):
    """Ajan araçları çağırır, ilk notta uydurma kaynak var -> denetim geri gönderir -> düzeltilmiş not temiz."""
    ctx = _ctx(tmp_path)
    sess, _ = inceleme.yeni_oturum(ctx.db, sirket=SID, ews_dir=ctx.ews.dir, http=ctx.http)
    goren_araclar = []
    adimlar = iter([
        {"tool_calls": [_arac("ews_ozet", {}, 1), _arac("ews_haberler", {}, 2)]},
        {"tool_calls": [_arac("cevap_ver", {"cevap": "1. Özet\n- Rekabet Kurumu soruşturması açık ve sürüyor [K2]\n"
                                                     "- Şirket konkordatoya başvurdu ve mühlet aldı [K7]"}, 3)]},
        {"tool_calls": [_arac("cevap_ver", {"cevap": "1. Özet\n- Rekabet Kurumu soruşturması açık ve sürüyor [K2]\n"
                                                     "5. Eksik bilgi\n- Konkordato bilgisi kaynaklarda yok"}, 4)]},
    ])

    def fake_llm(messages, tools=None, no_think=False):
        goren_araclar.append(sorted(t["function"]["name"] for t in tools))
        msg = dict(next(adimlar))
        msg.setdefault("content", "")
        return msg

    monkeypatch.setattr(core, "llm_chat", fake_llm)
    monkeypatch.setattr(core, "LOG_PATH", str(tmp_path / "log.jsonl"))
    durum = inceleme.incele(sess, ctx)
    assert durum == "done"
    assert "ews_ozet" in goren_araclar[0] and "sql_calistir" not in goren_araclar[0]   # inceleme araç seti
    assert any("K7" in m["content"] for m in sess.messages if m["role"] == "user")   # denetim geri gönderdi
    assert ctx.denetim == [] and "[K7]" not in sess.result["cevap"]
    assert len(inceleme.kaynak_listesi(ctx)) == len(ctx.kaynaklar) >= 3


# ------------------------------- KAP -------------------------------
import kap_kaynak  # noqa: E402

_GOVDE = "<p>Şirketimiz İstanbul Asliye Ticaret Mahkemesine konkordato başvurusunda bulunmuştur.</p>"


def _flight_html(ix, kod):
    temel = {"disclosureBasic": {"disclosureIndex": ix, "publishDate": "20.11.2026 10:00:00", "title": "Özel Durum Açıklaması",
                                 "stockCode": kod, "disclosureClass": "ODA", "summary": "Konkordato başvurusu"},
             "disclosureBody": ["$2"]}
    govde_bayt = len(_GOVDE.encode("utf-8"))           # T<hex> uzunluğu BAYT cinsindendir (Türkçe karakterler)
    chunk = "1:" + json.dumps(temel, ensure_ascii=False) + "\n2:T" + format(govde_bayt, "x") + "," + _GOVDE
    return "<html><script>self.__next_f.push([1," + json.dumps(chunk, ensure_ascii=False) + "])</script></html>"


def test_kap_flight_parse_multibyte_body():
    by_id = kap_kaynak.kapweb_next_data(_flight_html(555, "ORNEK"))
    assert "konkordato başvurusunda" in kap_kaynak.kapweb_resolve_body(by_id)
    recs = []
    kap_kaynak.kapweb_collect_records(by_id, recs)
    assert recs[0]["disclosureIndex"] == 555


class FakeKapSession:
    """kap.org.tr: ana sayfa, şirket araması, gün gün liste, detay sayfası."""

    def __init__(self):
        self.headers, self.proxies, self.verify, self.calls = {}, {}, True, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/tr"):
            return FakeResp(200, content=b"ok")
        r = FakeResp(200)
        r.text = _flight_html(555, "ORNEK")
        return r

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        if url.endswith("/search/combined"):
            return FakeResp(200, [{"category": "companyOrFunds", "results": [
                {"searchValue": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.", "searchType": "C", "memberOrFundOid": "oid1",
                 "cmpOrFundCode": "ornek"}]}])
        gun = json["fromDate"]
        kayitlar = [{"disclosureBasic": {"disclosureIndex": 1, "stockCode": "BASKA", "companyTitle": "BAŞKA A.Ş.",
                                         "mkkMemberOid": "x", "title": "Rutin", "publishDate": gun + " 09:00:00"}}]
        if gun == FakeKapSession.hedef_gun:
            kayitlar.append({"disclosureBasic": {"disclosureId": "4028abcdef0123", "disclosureIndex": 555,
                                                 "stockCode": "ORNEK", "mkkMemberOid": "oid1",
                                                 "companyTitle": "ÖRNEK ÇELİK SANAYİ VE TİCARET A.Ş.",
                                                 "title": "Özel Durum Açıklaması", "disclosureClass": "ODA",
                                                 "summary": "Konkordato başvurusu", "publishDate": gun + " 10:00:00"}})
        return FakeResp(200, kayitlar)


def test_kap_ara_filters_in_memory_and_reads_body(tmp_path, monkeypatch):
    import datetime as _dt
    FakeKapSession.hedef_gun = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%d.%m.%Y")
    monkeypatch.setattr(kap_kaynak, "KAPWEB_SLEEP", 0)
    ctx = _ctx(tmp_path)
    fake = FakeKapSession()
    ctx._kap = kap_kaynak.KapClient(fake)
    once = set(os.listdir(tmp_path))
    out = ctx.kap_ara(gun=3)
    assert "ÖRNEK ÇELİK" in out and "Rutin" not in out and "konkordato başvurusunda" in out   # başka şirket elendi
    gunler = [c[2]["fromDate"] for c in fake.calls if c[0] == "POST" and c[1].endswith("/list/main")]
    assert len(gunler) == 4                                                                     # gün gün (bugün + 3)
    assert any(c[1].endswith("/tr/Bildirim/555") for c in fake.calls if c[0] == "GET")
    k = [v for v in ctx.kaynaklar.values() if v["tur"] == "KAP"][0]
    dun_iso = _dt.datetime.strptime(FakeKapSession.hedef_gun, "%d.%m.%Y").strftime("%Y-%m-%d")
    assert k["url"] == "https://kap.org.tr/tr/Bildirim/555" and k["tarih"] == dun_iso    # tarih liste kaydından
    assert set(os.listdir(tmp_path)) == once                                                   # diske bir şey yazılmadı


# ------------------------------- DWH şablonları -------------------------------
class FakeCursor:
    def __init__(self, log):
        self.log, self.description = log, [("PART_ID",), ("NAKIT",)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchmany(self, n):
        return [(202609, 1.5e9), (202610, 1.4e9)]


class FakeConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return FakeCursor(self.log)


def test_dwh_sorgu_only_templates_with_bind(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(inceleme, "INCELEME_SORGULARI", {})
    assert "tanımlı değil" in ctx.dwh_sorgu("x")
    sablon = {"bakiye": {"aciklama": "aylık bakiye", "sql": "SELECT part_id, nakit FROM S.T WHERE counterparty_id = :cid"}}
    monkeypatch.setattr(inceleme, "INCELEME_SORGULARI", sablon)
    assert "bakiye: aylık bakiye" in ctx.dwh_sorgu("")                     # ad yoksa liste
    assert "bağlantısı yok" in ctx.dwh_sorgu("bakiye")
    ctx.conn = FakeConn()
    out = ctx.dwh_sorgu("bakiye")
    assert "202609" in out and re.match(r"^\[K\d+\] DWH 'bakiye'", out)
    sql, params = ctx.conn.log[0]
    assert params == {"cid": SID} and str(SID) not in sql                  # numara SQL'e yazılmadı, bind edildi
    sablon["kotu"] = {"aciklama": "x", "sql": "DELETE FROM S.T WHERE counterparty_id = :cid"}
    assert "SELECT" in _hata(lambda: ctx.dwh_sorgu("kotu"))                 # yalnızca SELECT


def test_empty_result_gets_source_and_failed_correction_keeps_first_note(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(inceleme, "INCELEME_SORGULARI", {})
    yok = ctx.dwh_sorgu()
    assert yok.startswith("[K1]") and ctx.kaynaklar["K1"]["tur"] == "DWH"            # "yok" bulgusu atıf alabilir
    sess, _ = inceleme.yeni_oturum(ctx.db, sirket=SID, ews_dir=ctx.ews.dir, http=ctx.http)
    adim = iter([{"tool_calls": [_arac("cevap_ver", {"cevap": "1. Özet\n- Şirket hakkında uydurma bir iddia burada [K9]"}, 1)]}])

    def fake_llm(messages, tools=None, no_think=False):
        msg = dict(next(adim))        # ikinci turda StopIteration -> core.run 'error' döner
        msg.setdefault("content", "")
        return msg

    monkeypatch.setattr(core, "llm_chat", fake_llm)
    monkeypatch.setattr(core, "LOG_PATH", str(tmp_path / "log.jsonl"))
    assert inceleme.incele(sess, ctx) == "done"
    assert "[K9]" in sess.result["cevap"]                                              # ilk not korundu
    assert any("K9" in d for d in ctx.denetim) and any("Düzeltme turu" in d for d in ctx.denetim)


def test_ayarlar_inceleme_dosyasi_once_okunur(monkeypatch):
    import importlib
    monkeypatch.setitem(sys.modules, "ayarlar_inceleme", types.SimpleNamespace(KAP_GUN=7, DIS_PROXY="http://p:1"))
    mod = importlib.reload(inceleme)
    try:
        assert mod.KAP_GUN == 7 and mod.DIS_PROXY == "http://p:1" and mod.KAP_DETAY == 15
    finally:
        monkeypatch.delitem(sys.modules, "ayarlar_inceleme")
        importlib.reload(inceleme)
