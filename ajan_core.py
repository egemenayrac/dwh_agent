# -*- coding: utf-8 -*-
"""
ajan_core.py - Veri ambarı ajanının beyni (arayüzden bağımsız)

ajan_app.py (Streamlit arayüzü) bunu kullanır. İçinde:
  - LLM istemcisi (IBThink, native tool calling)
  - Araçlar: bilgi_ara, tablo_karti, join_yolu, koken, son_donem, sql_calistir, tekillik_kontrol,
             plan_onayla, kullaniciya_sor, cevap_ver
  - Ajan döngüsü: durdurulabilir / devam ettirilebilir (insan onayı için)
  - Doğrulanmış sorgu kütüphanesi, log, ölçüm

Bilgi tabanı (kb.sqlite) kb_builder / kb_profile / kb_annotate ile oluşturulur.
"""
import datetime as dt
import json
import math
import os
import re
import sqlite3
import ssl
import sys
import tempfile
import threading
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_builder as kb  # noqa: E402  (KB_DIR, PERSONAL_REGEX, family_of, read_text)

# =============================== AYARLAR ===============================
LLM_URL = kb.LLM_URL             # ayarlar.py'den gelir
LLM_API_KEY = ""               # boş bırak: anahtar ibthink_anahtar.txt dosyasından ya da arayüzden alınır
LLM_MODEL = kb.LLM_MODEL
LLM_VERIFY_SSL = "windows"
LLM_TIMEOUT = 600
LLM_MAX_TOKENS = 4096
TEMPERATURE = 0.1

MAX_STEPS = 30                 # bir soru için en fazla LLM adımı
TOOL_OUTPUT_CHARS = 9000       # araç sonucunun modele giden en fazla karakteri
SHOW_ROWS = 5000               # ekranda tutulacak en fazla satır (sql_calistir)
MODEL_ROWS = 25                # modele gösterilen örnek satır sayısı
QUERY_TIMEOUT_MS = 180_000     # tek sorgu zaman aşımı
EXPORT_MAX_ROWS = 500_000      # "tüm sonucu Excel'e" için üst sınır
EVAL_MAX_ROWS = 50_000         # ölçümde altın SQL ve ajan SQL'i için ortak üst sınır
HEAVY_COST = 5_000_000         # EXPLAIN PLAN maliyeti bunu aşarsa uyarı
HEAVY_ROWS = 50_000_000        # tahmini satır sayısı bunu aşarsa uyarı
CTAS_TIMEOUT_MS = 30 * 60_000  # tablo oluşturma (CREATE TABLE AS SELECT) zaman aşımı
# ======================================================================

KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibthink_anahtar.txt")
KB_PATH = os.path.join(kb.KB_DIR, "kb.sqlite")
LOG_PATH = os.path.join(kb.KB_DIR, "ajan_log.jsonl")
PAUSE_TOOLS = {"plan_onayla", "kullaniciya_sor"}
_HTTP_LOCAL = threading.local()
_DB_LOCK = threading.Lock()


def _http():
    """Her iş parçacığı için ayrı kalıcı requests.Session döner (thread-safe Keep-Alive)."""
    sess = getattr(_HTTP_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        _HTTP_LOCAL.session = sess
    return sess

_TR_FOLD_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
})


def tr_fold(text):
    """Türkçe büyük/küçük harf ve aksan farkını kaldıran arama katlaması (SQLite TR_FOLD için)."""
    if text is None:
        return ""
    return str(text).translate(_TR_FOLD_MAP).lower()


# ------------------------------- LLM -------------------------------
def _verify():
    if LLM_VERIFY_SSL != "windows":
        return LLM_VERIFY_SSL
    if not hasattr(ssl, "enum_certificates"):
        return True
    pems = {ssl.DER_cert_to_PEM_cert(c) for s in ("ROOT", "CA")
            for c, enc, _ in ssl.enum_certificates(s) if enc == "x509_asn"}
    path = os.path.join(tempfile.gettempdir(), "ajan_windows_ca.pem")
    with open(path, "w", encoding="ascii") as f:
        f.write("".join(sorted(pems)))
    return path


VERIFY = _verify()


def get_api_key():
    """Anahtar sırası: koddaki LLM_API_KEY, IBTHINK_API_KEY ortam değişkeni, ibthink_anahtar.txt dosyası."""
    if LLM_API_KEY:
        return LLM_API_KEY
    if os.getenv("IBTHINK_API_KEY"):
        return os.getenv("IBTHINK_API_KEY")
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8").read().strip()
    return ""


def save_api_key(key):
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key.strip())


def strip_think(text):
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    return re.sub(r"(?s)^.*?</think>", "", text).strip()


def llm_chat(messages, tools=None, no_think=False):
    """Bir LLM isteği; mesaj sözlüğü döner. Geçici hatalarda bekleyip tekrar dener."""
    msgs = messages
    if no_think:
        msgs = messages + [{"role": "user", "content": "Kısa ve öz devam et. /no_think"}]
    payload = {"messages": msgs, "temperature": TEMPERATURE, "max_tokens": LLM_MAX_TOKENS, "stream": False}
    if LLM_MODEL:
        payload["model"] = LLM_MODEL
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    key = re.sub(r"^bearer\s+", "", get_api_key(), flags=re.I)
    if not key:
        raise RuntimeError(f"LLM API anahtarı yok. Sol panelden anahtarı gir ({kb.API_KEY_PAGE or 'anahtar sayfası'}).")
    headers = {"Authorization": f"Bearer {key}"}
    last = ""
    sess = _http()
    for attempt in range(5):
        try:
            r = sess.post(LLM_URL, json=payload, headers=headers, timeout=LLM_TIMEOUT, verify=VERIFY)
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {str(e)[:200]}"
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            ra = r.headers.get("Retry-After", "")
            time.sleep(int(ra) if ra.isdigit() else 20 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"IBThink anahtarı reddedildi (HTTP {r.status_code}). Sol panelden anahtarı kontrol et.")
        if not r.ok:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:400]}")
        choice = (r.json().get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        msg["_finish"] = choice.get("finish_reason")
        return msg
    raise RuntimeError(f"LLM'e ulaşılamadı: {last}")


# ---------------------------- Bilgi tabanı ----------------------------
VERIFIED_SQL = """CREATE TABLE IF NOT EXISTS verified_queries(id INTEGER PRIMARY KEY, soru TEXT, sql TEXT,
    aciklama TEXT, tablolar TEXT, created_at TEXT)"""
FEEDBACK_SQL = """CREATE TABLE IF NOT EXISTS feedback_log(id INTEGER PRIMARY KEY, soru TEXT, sql TEXT,
    sonuc TEXT, notlar TEXT, created_at TEXT)"""


def init_db(db):
    """SQLite bağlantısına Türkçe arama fonksiyonunu, eşzamanlılık ayarlarını ve ajan tablolarını ekler."""
    db.create_function("TR_FOLD", 1, tr_fold)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
    except sqlite3.Error:
        pass
    with _DB_LOCK:
        db.execute(VERIFIED_SQL)
        db.execute(FEEDBACK_SQL)
        db.commit()
    return db


def open_kb():
    db = sqlite3.connect(KB_PATH, timeout=30.0, check_same_thread=False)
    return init_db(db)


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def onayli_bilgiler():
    path = os.path.join(kb.KB_DIR, "onayli_bilgiler.txt")
    if not os.path.exists(path):
        return ""
    facts = [l.strip() for l in kb.read_text(path).splitlines() if l.strip() and not l.strip().startswith("#")]
    return "\n".join(f"- {x}" for x in facts)


def short(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + " …"


def kb_stats(db):
    q = lambda s: db.execute(s).fetchone()[0]  # noqa: E731
    out = {"tablo": q("SELECT COUNT(*) FROM objects"), "kullanilan": q("SELECT COUNT(*) FROM v_usage"),
           "join": q("SELECT COUNT(*) FROM join_edges") if has_table(db, "join_edges") else 0,
           "kart": q("SELECT COUNT(*) FROM table_cards WHERE status='ok'") if has_table(db, "table_cards") else 0,
           "onayli_sorgu": q("SELECT COUNT(*) FROM verified_queries"),
           "geri_bildirim": q("SELECT COUNT(*) FROM feedback_log") if has_table(db, "feedback_log") else 0}
    return out


# ------------------------------- Oracle -------------------------------
def oracle_connect(user, password):
    import oracledb
    oracledb.defaults.fetch_lobs = False
    conn = oracledb.connect(user=user, password=password, dsn=kb.DB_DSN)
    conn.call_timeout = QUERY_TIMEOUT_MS
    return conn


FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|execute|begin|declare|"
                       r"commit|rollback|lock|call|rename|purge|flashback)\b", re.I)


def check_select(sql):
    """Sadece tek bir SELECT/WITH sorgusuna izin verir. Asıl koruma: veritabanı kullanıcısının salt okunur olması."""
    s = (sql or "").strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ValueError("Sadece SELECT veya WITH ile başlayan sorgular çalıştırılabilir.")
    try:
        import sqlglot
        stmts = [x for x in sqlglot.parse(s, read="oracle") if x is not None]
        if len(stmts) != 1:
            raise ValueError("Tek seferde tek sorgu çalıştırılabilir.")
        if stmts[0].key not in ("select", "union", "intersect", "except", "with", "subquery"):
            raise ValueError(f"Sadece okuma sorgusu çalıştırılabilir (bulunan: {stmts[0].key}).")
    except ValueError:
        raise
    except Exception:
        pass  # sqlglot bu Oracle sözdizimini çözemedi: kelime kontrolüne düş
    clean = re.sub(r"'(?:''|[^'])*'", "''", re.sub(r"--[^\n]*|/\*.*?\*/", " ", s, flags=re.S))
    if ";" in clean:
        raise ValueError("Tek seferde tek sorgu çalıştırılabilir (';' kullanma).")
    m = FORBIDDEN.search(clean)
    if m:
        raise ValueError(f"Sorguda izin verilmeyen kelime var: {m.group(0)}")
    return s


def explain_cost(conn, sql):
    """EXPLAIN PLAN ile tahmini maliyet ve satır sayısı; alınamazsa (None, None)."""
    sid = f"AJ{int(time.time() * 1000) % 10**9}"
    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' FOR {sql}")
            cur.execute("SELECT cost, cardinality FROM plan_table WHERE statement_id = :s AND id = 0", {"s": sid})
            row = cur.fetchone()
        conn.rollback()
        return (row[0], row[1]) if row else (None, None)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, None


def fetch_df(conn, sql, max_rows):
    with conn.cursor() as cur:
        cur.arraysize = 2000
        cur.prefetchrows = 2000
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    return pd.DataFrame(rows[:max_rows], columns=cols), len(rows) > max_rows


def export_full(conn, sql):
    s = check_select(sql)
    old = conn.call_timeout
    conn.call_timeout = 30 * 60_000
    try:
        df, more = fetch_df(conn, s, EXPORT_MAX_ROWS)
    finally:
        conn.call_timeout = old
    return df, more


# ------------------------------- Araçlar -------------------------------
def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
TOOLS = [
    _fn("bilgi_ara", "Bilgi tabanında arar: ilgili tablolar (kartları ve kullanım sayılarıyla), ekibin onayladığı "
        "sorgular, ekibin benzer geçmiş sorguları ve dönemsel tablo aileleri. İlk adım budur. Türkçe ve İngilizce "
        "eş anlamlıları ve kısaltmaları da ver (ör. gecikme, delay, dpd, dlq).",
        {"kelimeler": {"type": "array", "items": S}}, ["kelimeler"]),
    _fn("tablo_karti", "Bir tablonun kartı: ne tuttuğu, satır seviyesi, kolonlar (tip, aralık, kod değerleri), "
        "doğrulanmış join'leri ve tuzakları. SQL yazmadan önce kullanacağın her tablo için çağır.",
        {"tablo": {"type": "string", "description": "OWNER.TABLO"}}, ["tablo"]),
    _fn("join_yolu", "İki tablo arasındaki doğrulanmış join yolunu (ekibin sorgularından) verir. Join'i ASLA "
        "tahmin etme, bu aracı kullan.", {"tablo1": S, "tablo2": S}, ["tablo1", "tablo2"]),
    _fn("koken", "Tablonun hangi tablolardan üretildiğini ve hangi tabloları beslediğini verir.", {"tablo": S},
        ["tablo"]),
    _fn("son_donem", "Dönemsel tablo ailesinin (ör. PD_MAPPING, SME_WL) en yeni tablosunu ve dönemlerini verir.",
        {"ad": S}, ["ad"]),
    _fn("sql_calistir", "Oracle'da SELECT sorgusu çalıştırır, satır sayısını ve ilk satırları döner. Ağır sorguda "
        "önce uyarı verir; kullanıcı onaylarsa zorla=true ile tekrar çağır.",
        {"sql": S, "amac": S, "zorla": {"type": "boolean"}}, ["sql", "amac"]),
    _fn("tekillik_kontrol", "Join'li sorgunun satırları çoğaltıp çoğaltmadığını kontrol eder: COUNT(*) ile anahtar "
        "kolonların COUNT(DISTINCT) değerini karşılaştırır.",
        {"sql": S, "anahtar_kolonlar": {"type": "array", "items": S}}, ["sql", "anahtar_kolonlar"]),
    _fn("plan_onayla", "Karmaşık sorularda (2+ tablo, çok adımlı hesap, dönem karşılaştırması) SQL yazmadan önce "
        "planı kullanıcıya onaylatır. Kullanıcının cevabını döner.", {"plan": S}, ["plan"]),
    _fn("kullaniciya_sor", "Kullanıcıya soru sorar ve cevabını döner: tablo seçimini onaylatmak, belirsizliği "
        "gidermek, ağır sorgu için izin almak.", {"soru": S}, ["soru"]),
    _fn("tablo_olustur", "SADECE kullanıcı açıkça tablo oluşturmanı isterse: verilen SELECT'in sonucuyla "
        "kullanıcının KENDİ şemasında yeni bir tablo oluşturur (CREATE TABLE ... AS SELECT). Var olan tablonun "
        "üzerine yazmaz. Önce SELECT'i sql_calistir ile dene. Kullanıcı ekranda onaylayınca çalışır.",
        {"tablo_adi": S, "sql": S, "aciklama": S, "zorla": {"type": "boolean"}}, ["tablo_adi", "sql"]),
    _fn("cevap_ver", "İş bitince çağrılır. Kısa Türkçe cevap, kullanılan nihai SQL (yoksa boş) ve varsayımlar.",
        {"cevap": S, "sql": S, "varsayimlar": S}, ["cevap"]),
]


class Ctx:
    """Araçların ihtiyaç duyduğu bağlantılar ve oturum durumu."""

    def __init__(self, db, conn=None, use_verified=True):
        self.db, self.conn = db, conn
        self.use_verified = use_verified
        self.last_df, self.last_sql, self.last_more = None, None, False
        self.created = []
        self._graph = None

    # ---- yardımcılar ----
    def resolve(self, tablo):
        t = (tablo or "").strip().strip('"').upper()
        if "." in t:
            o, n = t.split(".", 1)
            if self.db.execute("SELECT 1 FROM objects WHERE owner=? AND name=?", (o, n)).fetchone():
                return o, n
            t = n
        row = self.db.execute("""SELECT o.owner FROM objects o LEFT JOIN v_usage u ON u.owner=o.owner
                                 AND u.table_name=o.name WHERE o.name=? ORDER BY COALESCE(u.n_files,0) DESC LIMIT 1""",
                              (t,)).fetchone()
        return (row[0], t) if row else (None, t)

    def graph(self):
        if self._graph is None:
            import networkx as nx
            g, personal = nx.Graph(), re.compile(kb.PERSONAL_REGEX)
            for o1, t1, c1, o2, t2, c2, n in self.db.execute(
                    "SELECT owner1, table1, col1, owner2, table2, col2, n_files FROM join_edges"):
                a, b = f"{o1}.{t1}", f"{o2}.{t2}"
                w = 1.0 / (1 + (n or 0)) + (0.5 if personal.match(o1) or personal.match(o2) else 0)
                if g.has_edge(a, b):
                    g[a][b]["conds"].append((a, c1, b, c2))
                    g[a][b]["weight"] = min(g[a][b]["weight"], w)
                else:
                    g.add_edge(a, b, conds=[(a, c1, b, c2)], weight=w, n=n)
            self._graph = g
        return self._graph

    # ---- araçlar ----
    def bilgi_ara(self, kelimeler):
        terms = [k.strip() for k in (kelimeler or []) if k and k.strip()][:12]
        if not terms:
            return "Arama kelimesi ver."
        db, out = self.db, []
        scores = {}
        has_cards = has_table(db, "table_cards")
        for t in terms:
            tf = tr_fold(t)
            like = f"%{tf}%"
            for o, n, cmt, nf in db.execute("""
                    SELECT o.owner, o.name, o.comments, COALESCE(u.n_files,0) FROM objects o
                    LEFT JOIN v_usage u ON u.owner=o.owner AND u.table_name=o.name
                    WHERE TR_FOLD(o.name) LIKE ? OR TR_FOLD(COALESCE(o.comments,'')) LIKE ?
                    ORDER BY COALESCE(u.n_files,0) DESC LIMIT 60""", (like, like)):
                k = (o, n)
                scores[k] = scores.get(k, 0) + (2 if tf in tr_fold(n) else 1)
            if has_cards:
                for o, n in db.execute("""SELECT owner, table_name FROM table_cards WHERE status='ok'
                                          AND TR_FOLD(card) LIKE ?""", (like,)):
                    scores[(o, n)] = scores.get((o, n), 0) + 1.5
        personal = re.compile(kb.PERSONAL_REGEX)
        ranked = []
        for (o, n), sc in scores.items():
            nf = (db.execute("SELECT n_files FROM v_usage WHERE owner=? AND table_name=?", (o, n)).fetchone()
                  or (0,))[0]
            ranked.append((sc + math.log1p(nf) - (0.7 if personal.match(o) else 0), o, n, nf))
        ranked.sort(reverse=True)
        out.append("İLGİLİ TABLOLAR (puan sırasıyla):")
        for sc, o, n, nf in ranked[:12]:
            ozet = ""
            if has_cards:
                row = db.execute("SELECT card FROM table_cards WHERE owner=? AND table_name=? AND status='ok'",
                                 (o, n)).fetchone()
                if row:
                    c = json.loads(row[0])
                    ozet = f" | {short(c.get('ozet'), 160)} | seviye: {short(c.get('satir_seviyesi'), 60)}"
            if not ozet:
                cmt = db.execute("SELECT comments FROM objects WHERE owner=? AND name=?", (o, n)).fetchone()
                ozet = f" | {short(cmt[0], 120)}" if cmt and cmt[0] else ""
            out.append(f"- {o}.{n} ({nf} dosyada kullanılıyor){ozet}")
        if not ranked:
            out.append("- (eşleşen tablo yok; başka kelimeler dene)")

        if self.use_verified:
            ver = []
            for t in terms:
                ver += db.execute("SELECT id, soru, sql FROM verified_queries WHERE TR_FOLD(soru) LIKE ? LIMIT 3",
                                  (f"%{tr_fold(t)}%",)).fetchall()
            if ver:
                out.append("\nEKİBİN ONAYLADIĞI SORGULAR (öncelikle bunları örnek al):")
                for _, soru, sql in {v[0]: v for v in ver}.values():
                    out.append(f"- Soru: {soru}\n  SQL: {short(sql, 900)}")

        if has_table(db, "snippets_fts"):
            q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            try:
                rows = db.execute("""SELECT s.id, s.text, COUNT(DISTINCT ss.source) n FROM snippets_fts f
                                     JOIN snippets s ON s.id=f.rowid JOIN snippet_sources ss ON ss.snippet_id=s.id
                                     WHERE snippets_fts MATCH ? GROUP BY s.id ORDER BY bm25(snippets_fts) LIMIT 5""",
                                  (q,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                out.append("\nEKİBİN BENZER GEÇMİŞ SORGULARI (doğrulanmamış örnekler):")
                for _, text, n in rows:
                    flat = re.sub(r"\s+", " ", text)
                    out.append(f"- ({n} dosyada) {short(flat, 700)}")

        fams = []
        for t in terms:
            fams += db.execute("SELECT owner, family, n_tables, last_table FROM families WHERE TR_FOLD(family) LIKE ? LIMIT 3",
                               (f"%{tr_fold(t)}%",)).fetchall()
        if fams:
            out.append("\nDÖNEMSEL TABLO AİLELERİ:")
            for o, f, n, last in dict.fromkeys(fams):
                out.append(f"- {o}.{f}: {n} dönem, en yenisi {o}.{last}")
        return "\n".join(out)

    def tablo_karti(self, tablo):
        db = self.db
        o, n = self.resolve(tablo)
        if not o:
            return f"{tablo} bulunamadı. bilgi_ara ile doğru adı bul."
        out = [f"TABLO: {o}.{n}"]
        obj = db.execute("SELECT type, comments, num_rows, last_ddl, mod_time FROM objects WHERE owner=? AND name=?",
                         (o, n)).fetchone()
        out.append(f"Tip: {obj[0]} | Satır (istatistik): {obj[2]} | Açıklama: {obj[1] or '-'}")
        if has_table(db, "table_cards"):
            row = db.execute("SELECT card FROM table_cards WHERE owner=? AND table_name=? AND status='ok'",
                             (o, n)).fetchone()
            if row:
                c = json.loads(row[0])
                out.append(f"Özet: {c.get('ozet')}\nSatır seviyesi: {c.get('satir_seviyesi')}")
                out.append("Anahtar kolonlar: " + ", ".join(map(str, c.get("anahtar_kolonlar") or [])))
                for d in c.get("tarih_kolonlari") or []:
                    if isinstance(d, dict):
                        out.append(f"Tarih kolonu {d.get('kolon')}: {d.get('anlam')}")
                for d in (c.get("onemli_kolonlar") or [])[:15]:
                    if isinstance(d, dict):
                        out.append(f"Kolon {d.get('kolon')}: {d.get('anlam')}")
                for t in c.get("tuzaklar") or []:
                    out.append(f"TUZAK: {t}")
            else:
                out.append("(Bu tablonun kartı henüz yok; kolonlara ve örnek sorgulara dayan.)")
        prof = {}
        if has_table(db, "col_profile"):
            prof = {c: (lo, hi, tv) for c, lo, hi, tv in db.execute(
                "SELECT column_name, low, high, top_values FROM col_profile WHERE owner=? AND table_name=?", (o, n))}
        lk = {}
        if has_table(db, "lookup_values"):
            for col, code, descr in db.execute(
                    "SELECT column_name, code, descr FROM lookup_values WHERE owner=? AND table_name=?", (o, n)):
                lk.setdefault(col, {})[code] = descr
        cols = db.execute("""SELECT column_name, data_type, num_distinct, comments FROM columns
                             WHERE owner=? AND table_name=? ORDER BY column_id""", (o, n)).fetchall()
        out.append(f"\nKOLONLAR ({len(cols)}):")
        for c, t, nd, cm in cols[:150]:
            lo, hi, tv = prof.get(c, (None, None, None))
            if tv and c in lk:
                tv = ", ".join((f"{p.rpartition(' ')[0]}={short(lk[c].get(p.rpartition(' ')[0]), 30)} "
                                f"{p.rpartition(' ')[2]}") if p.rpartition(" ")[0] in lk[c] else p
                               for p in tv.split(", "))
            out.append(f"{c} {t} distinct={nd}" + (f" aralık={lo}..{hi}" if lo or hi else "")
                       + (f" değerler=[{tv}]" if tv else "") + (f" -- {short(cm, 80)}" if cm else ""))
        if len(cols) > 150:
            out.append(f"… ve {len(cols) - 150} kolon daha")
        joins = db.execute("""SELECT owner1, table1, col1, owner2, table2, col2, n_files FROM join_edges
                              WHERE (owner1=? AND table1=?) OR (owner2=? AND table2=?) ORDER BY n_files DESC LIMIT 20""",
                           (o, n, o, n)).fetchall()
        if joins:
            out.append("\nDOĞRULANMIŞ JOIN'LER:")
            for o1, t1, c1, o2, t2, c2, nf in joins:
                out.append(f"- {o1}.{t1}.{c1} = {o2}.{t2}.{c2} ({nf} dosya)")
        fam = kb.family_of(n)
        if fam:
            f = db.execute("SELECT n_tables, last_table FROM families WHERE owner=? AND family=?", (o, fam)).fetchone()
            if f:
                out.append(f"\nDönemsel aile {o}.{fam}: {f[0]} dönem, en yenisi {o}.{f[1]}")
        return "\n".join(out)

    def join_yolu(self, tablo1, tablo2):
        import networkx as nx
        a, b = ("{}.{}".format(*self.resolve(t)) for t in (tablo1, tablo2))
        g = self.graph()
        if a not in g or b not in g:
            eksik = [x for x in (a, b) if x not in g]
            return f"{', '.join(eksik)} için doğrulanmış join yok. Ortak kolon adlarına bakıp kullanıcıya sor."
        try:
            path = nx.shortest_path(g, a, b, weight="weight")
        except nx.NetworkXNoPath:
            return f"{a} ile {b} arasında doğrulanmış join yolu yok."
        out = [f"YOL: {' -> '.join(path)}"]
        for x, y in zip(path, path[1:]):
            e = g[x][y]
            conds = " AND ".join(f"{p}.{c} = {q}.{d}" for p, c, q, d in e["conds"])
            out.append(f"- {x} ile {y}: {conds} ({e.get('n')} dosyada kullanılmış)")
        return "\n".join(out)

    def koken(self, tablo):
        o, n = self.resolve(tablo)
        up = self.db.execute("""SELECT DISTINCT source_owner||'.'||source_table FROM v_lineage
                                WHERE target_owner=? AND target_table=? LIMIT 25""", (o, n)).fetchall()
        down = self.db.execute("""SELECT DISTINCT target_owner||'.'||target_table FROM v_lineage
                                  WHERE source_owner=? AND source_table=? LIMIT 25""", (o, n)).fetchall()
        return (f"{o}.{n} şunlardan üretiliyor: {', '.join(r[0] for r in up) or '-'}\n"
                f"{o}.{n} şunları besliyor: {', '.join(r[0] for r in down) or '-'}")

    def son_donem(self, ad):
        key = (ad or "").upper().split(".")[-1]
        key = re.sub(r"_?\{?DONEM\}?", "", key).strip("_")
        rows = self.db.execute("""SELECT owner, family, n_tables, first_table, last_table FROM families
                                  WHERE family LIKE ? ORDER BY n_tables DESC LIMIT 5""", (f"%{key}%",)).fetchall()
        if not rows:
            return f"'{ad}' adında dönemsel tablo ailesi bulunamadı."
        return "\n".join(f"{o}.{f}: {n} dönem, ilk {o}.{first}, EN YENİ {o}.{last}" for o, f, n, first, last in rows)

    def sql_calistir(self, sql, amac="", zorla=False):
        if self.conn is None:
            return "Oracle bağlantısı yok. Kullanıcıdan sol paneldeki 'Bağlan' ile bağlanmasını iste."
        try:
            s = check_select(sql)
        except ValueError as e:
            return f"ÇALIŞTIRILMADI: {e}"
        if not zorla:
            cost, card = explain_cost(self.conn, s)
            if (cost and cost > HEAVY_COST) or (card and card > HEAVY_ROWS):
                return (f"UYARI: Sorgu ağır görünüyor (tahmini maliyet {cost}, tahmini satır {card}). Tarih/PART_ID "
                        f"filtresi eklemeyi dene ya da kullanıcıya sorup onay alırsan zorla=true ile çalıştır.")
        t0 = time.time()
        try:
            df, more = fetch_df(self.conn, s, SHOW_ROWS)
        except Exception as e:
            return f"ORACLE HATASI: {str(e)[:600]}"
        self.last_df, self.last_sql, self.last_more = df, s, more
        head = df.head(MODEL_ROWS).to_string(index=False, max_colwidth=40)
        return (f"{len(df)}{'+ (ilk ' + str(SHOW_ROWS) + ')' if more else ''} satır, {len(df.columns)} kolon, "
                f"{time.time() - t0:.1f} sn.\nKolonlar: {', '.join(df.columns)}\nİlk satırlar:\n{head}")

    def tablo_hazirla(self, tablo_adi, sql, aciklama="", zorla=False):
        """tablo_olustur için kontroller; (hazırlık sözlüğü, None) ya da (None, hata metni) döner."""
        if self.conn is None:
            return None, "Oracle bağlantısı yok. Kullanıcıdan sol paneldeki 'Bağlan' ile bağlanmasını iste."
        own = (self.conn.username or "").upper()
        ad = (tablo_adi or "").strip().strip('"').upper()
        if "." in ad:
            sema, ad = ad.split(".", 1)
            if sema.strip('"') != own:
                return None, f"ÇALIŞTIRILMADI: Tablo sadece kullanıcının kendi şemasında ({own}) oluşturulabilir."
            ad = ad.strip('"')
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", ad):
            return None, "ÇALIŞTIRILMADI: Geçersiz tablo adı (harfle başlamalı; harf, rakam ve _ kullan)."
        try:
            s = check_select(sql)
        except ValueError as e:
            return None, f"ÇALIŞTIRILMADI: {e}"
        try:
            var = fetch_df(self.conn, f"SELECT COUNT(*) FROM all_objects WHERE owner = '{own}' "
                                      f"AND object_name = '{ad}'", 1)[0].iloc[0, 0]
        except Exception as e:
            return None, f"ORACLE HATASI: {str(e)[:300]}"
        if var:
            return None, f"ÇALIŞTIRILMADI: {own}.{ad} zaten var. Üzerine yazılmaz; kullanıcıdan başka bir ad iste."
        cost, card = explain_cost(self.conn, s)
        if not zorla and ((cost and cost > HEAVY_COST) or (card and card > HEAVY_ROWS)):
            return None, (f"UYARI: Sorgu ağır görünüyor (tahmini maliyet {cost}, tahmini satır {card}). Filtre ekle "
                          f"ya da kullanıcı onaylarsa zorla=true ile tekrar çağır.")
        return {"owner": own, "ad": ad, "sql": s, "aciklama": aciklama or "", "satir_tahmini": card}, None

    def tablo_olustur_calistir(self, p):
        """Kullanıcı onayından sonra CREATE TABLE ... AS SELECT çalıştırır (yalnızca kendi şemasında, yeni tablo)."""
        if self.conn is None:
            return "Oracle bağlantısı yok."
        old, t0 = self.conn.call_timeout, time.time()
        hedef = '"' + p["owner"] + '"."' + p["ad"] + '"'
        self.conn.call_timeout = CTAS_TIMEOUT_MS
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"CREATE TABLE {hedef} AS {p['sql']}")
            n = fetch_df(self.conn, f"SELECT COUNT(*) FROM {hedef}", 1)[0].iloc[0, 0]
        except Exception as e:
            return f"ORACLE HATASI (tablo oluşturulamadı): {str(e)[:600]}"
        finally:
            self.conn.call_timeout = old
        ad = p["owner"] + "." + p["ad"]
        self.created.append(ad)
        return f"Tablo oluşturuldu: {ad} ({int(n):,} satır, {time.time() - t0:.0f} sn)."

    def tekillik_kontrol(self, sql, anahtar_kolonlar):
        if self.conn is None:
            return "Oracle bağlantısı yok."
        try:
            s = check_select(sql)
        except ValueError as e:
            return f"ÇALIŞTIRILMADI: {e}"
        keys = [re.sub(r"[^A-Za-z0-9_$#]", "", k) for k in anahtar_kolonlar or []][:5]
        if not keys:
            return "Anahtar kolon ver."
        expr = " || '~' || ".join(f'TO_CHAR("{k.upper()}")' for k in keys)
        try:
            df, _ = fetch_df(self.conn, f"SELECT COUNT(*) N, COUNT(DISTINCT {expr}) D FROM ({s})", 1)
        except Exception as e:
            return f"ORACLE HATASI: {str(e)[:400]}"
        n, d = int(df.iloc[0, 0]), int(df.iloc[0, 1])
        return (f"Toplam satır {n}, tekil anahtar {d}. " +
                ("Satır çoğalması YOK." if n == d else f"DİKKAT: {n - d} satır fazlalık var; join koşulu ya da tarih "
                                                       f"filtresi eksik olabilir."))


# ------------------------------ Ajan döngüsü ------------------------------
SYSTEM = """Sen bir bankanın kredi riski ekibi için çalışan uzman bir veri analisti ajanısın. Oracle veri ambarında
araçlarla çalışırsın. Bugünün tarihi: {today}. Veritabanında sadece okuma yaparsın; tek istisna tablo_olustur aracıdır.

ÇALIŞMA YÖNTEMİ
1. Önce bilgi_ara ile ilgili tabloları, onaylı ve benzer geçmiş sorguları bul. Türkçe/İngilizce eş anlamlıları ve
   kısaltmaları da ara (ör. batık/takip/tasfiye/NPL/liquidation/litigation; gecikme/delay/dpd/dlq).
2. Kullanacağın her tablo için tablo_karti çağır. Tablo veya kolon adı UYDURMA; sadece kartta gördüklerini kullan.
3. Tablolar arası bağlantı için join_yolu kullan. Join'i tahmin etme. Doğrulanmış yol yoksa kullaniciya_sor ile sor.
4. Soru karmaşıksa (2 veya daha fazla tablo, çok adımlı hesap, dönem karşılaştırması) SQL yazmadan ÖNCE plan_onayla
   ile 3-6 maddelik bir plan sun ve kullanıcının cevabına göre ilerle.
5. Seçtiğin tablolardan emin değilsen (kart yok, kanıt zayıf, birden çok aday var) kullaniciya_sor ile onaylat.
6. Tarihçe/snapshot tablolarında (AS_OF_DT, PART_ID vb.) mutlaka tarih veya dönem filtresi koy.
7. sql_calistir ile çalıştır. Join'li sorgularda tekillik_kontrol ile satır çoğalmasını kontrol et.
8. Hata gelirse nedenini düşünüp düzelt ve tekrar dene. Sonucun mantıklı olduğunu kontrol et.
9. Sadece tablo/kolon anlamı soruluyorsa SQL çalıştırma; kart bilgisiyle cevapla.
10. Bitince cevap_ver çağır: 3-10 satırlık tam bir Türkçe cevap (ana sonuç rakamlarla), nihai SQL ve varsayımlar.
    Sonuç tablosunu cevaba kopyalama; tablo ekranda ve Excel'de zaten görünüyor. Kullanıcının istemediği
    kırılımı (ör. ay bazında) ekleme; gerekirse sadece öner.
11. Kullanıcı açıkça "tablo oluştur / tabloya yaz / kaydet" derse: önce SELECT'i sql_calistir ile dene, sonra
    tablo_olustur çağır (kullanıcının kendi şemasında, yeni bir ad ile). Tablo adı verilmediyse kısa, anlamlı bir ad
    öner. INSERT/UPDATE/DELETE/DROP gibi başka hiçbir yazma işlemi yapamazsın.

ORACLE KURALLARI
- LIMIT yok: FETCH FIRST n ROWS ONLY. Tarih: TRUNC(SYSDATE), ADD_MONTHS(...), DATE '2026-01-31'.
- Tablo adlarını OWNER.TABLO yaz. Tablo alias'ında AS kullanma. NVL/COALESCE kullan.
- YYYYMMDD sayısal tarih kolonlarında sayı ile karşılaştır (ör. AS_OF_DT = 20261031) ya da TO_DATE ile çevir;
  kolon tipine kartta bak.
{facts}"""

MAIL_PROMPT = """Aşağıda bir birimden gelen veri talebi maili var. Mailden talebi ve (varsa) SQL'i çıkar.
- Mailde SQL varsa onu DEĞİŞTİRMEDEN sql_calistir ile çalıştır (sadece SELECT ise). Hata verirse düzeltme ÖNERME,
  hatayı raporla.
- Mailde SQL yoksa talebi normal bir soru gibi araçlarla çöz.
- cevap_ver'de 'cevap' alanına, talep edene gönderilecek kısa ve resmi bir Türkçe cevap maili taslağı yaz
  (ör. "Merhaba, talebiniz doğrultusunda sorgu çalıştırılmış olup sonuç ektedir (N satır)."). Varsayımları ayrıca yaz.

MAİL:
"""


class Session:
    """Bir sorunun çözüm durumu; durdurulup devam ettirilebilir."""

    def __init__(self, question, extra_system=""):
        facts = onayli_bilgiler()
        system = SYSTEM.format(today=dt.date.today().isoformat(),
                               facts=("\nEKİBİN ONAYLADIĞI BİLGİLER (kesindir):\n" + facts) if facts else "")
        self.question = question
        self.messages = [{"role": "system", "content": system + extra_system},
                         {"role": "user", "content": question}]
        self.steps, self.pending, self.result = [], None, None
        self.status, self.error, self.n_llm = "new", None, 0
        self.started = time.time()


def _exec_tool(ctx, name, args):
    fn = getattr(ctx, name, None)
    if fn is None or name in PAUSE_TOOLS or name == "cevap_ver":
        return f"Bilinmeyen araç: {name}"
    try:
        return str(fn(**args))
    except TypeError as e:
        return f"Hatalı parametre: {e}"
    except Exception as e:
        return f"Araç hatası: {type(e).__name__}: {str(e)[:300]}"


def _process_calls(sess, ctx, calls, on_step, auto_approve):
    """Araç çağrılarını sırayla çalıştırır. Onay gerektirende durur (pending) ve 'waiting' döner."""
    for i, c in enumerate(calls):
        name = c["function"]["name"]
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            msg = "Argümanlar okunamadı (çıktı yarıda kesilmiş). Bu aracı daha KISA argümanlarla tekrar çağır."
            sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": msg})
            sess.steps.append({"arac": name, "girdi": short(c["function"].get("arguments"), 300), "cikti": msg})
            continue
        if name == "cevap_ver" and len(str(args.get("cevap") or "").strip()) < 15:
            msg = ("cevap alanı boş ya da yarım geldi. cevap_ver'i tekrar çağır: 'cevap' alanına 3-10 satırlık tam bir "
                   "Türkçe cevap yaz (sonuç tablosunu cevaba kopyalama, ekranda zaten görünüyor).")
            sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": msg})
            sess.steps.append({"arac": name, "girdi": short(args.get("cevap"), 300), "cikti": msg})
            continue
        if name == "cevap_ver":
            sess.result = {"cevap": args.get("cevap", ""), "sql": args.get("sql") or ctx.last_sql or "",
                           "varsayimlar": args.get("varsayimlar", "")}
            sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": "Cevap kaydedildi."})
            sess.steps.append({"arac": name, "girdi": "", "cikti": str(args.get("cevap") or "")})
            return "done"
        if name == "tablo_olustur":
            prep, err = ctx.tablo_hazirla(**{k: v for k, v in args.items()
                                             if k in ("tablo_adi", "sql", "aciklama", "zorla")})
            if err or auto_approve:
                out = err or "Ölçüm/otomatik modda tablo oluşturulmaz."
                sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                sess.steps.append({"arac": name, "girdi": json.dumps(args, ensure_ascii=False), "cikti": out})
                if on_step:
                    on_step(sess.steps[-1])
                continue
            tahmin = f" (tahmini {int(prep['satir_tahmini']):,} satır)" if prep["satir_tahmini"] else ""
            text = (f"Ajan şemanda yeni bir tablo oluşturmak istiyor: **{prep['owner']}.{prep['ad']}**{tahmin}\n\n"
                    f"{prep['aciklama']}\n\n```sql\nCREATE TABLE {prep['owner']}.{prep['ad']} AS\n{prep['sql']}\n```\n"
                    f"Onaylarsan oluşturulur. İstemiyorsan düzeltmeni yazıp 'Düzeltmeyi gönder'e bas.")
            sess.pending = {"call_id": c["id"], "kind": name, "text": text, "rest": calls[i + 1:], "prep": prep}
            sess.steps.append({"arac": name, "girdi": json.dumps(args, ensure_ascii=False),
                               "cikti": "(kullanıcı onayı bekleniyor)"})
            if on_step:
                on_step(sess.steps[-1])
            return "waiting"
        if name in PAUSE_TOOLS:
            text = args.get("plan") or args.get("soru") or ""
            if auto_approve:
                answer = "Onaylandı, devam et."
                sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": answer})
                sess.steps.append({"arac": name, "girdi": text, "cikti": answer})
                continue
            sess.pending = {"call_id": c["id"], "kind": name, "text": text, "rest": calls[i + 1:]}
            sess.steps.append({"arac": name, "girdi": text, "cikti": "(kullanıcı cevabı bekleniyor)"})
            if on_step:
                on_step(sess.steps[-1])
            return "waiting"
        out = _exec_tool(ctx, name, args)
        sess.messages.append({"role": "tool", "tool_call_id": c["id"], "content": short(out, TOOL_OUTPUT_CHARS)})
        sess.steps.append({"arac": name, "girdi": json.dumps(args, ensure_ascii=False), "cikti": short(out, 1500)})
        if on_step:
            on_step(sess.steps[-1])
    return "continue"


def run(sess, ctx, on_step=None, auto_approve=False):
    """Döngüyü çalıştırır; 'done' | 'waiting' | 'error' döner."""
    sess.status = "running"
    try:
        while sess.n_llm < MAX_STEPS:
            msg = llm_chat(sess.messages, TOOLS)
            sess.n_llm += 1
            if msg.get("_finish") == "length":
                # 4096 token sınırı doldu: düşünme kısmı yeri bitirmiş, cevap/araç çağrısı yarıda kesilmiş olabilir.
                # Yarım çıktıyı atıp düşünmeden (kısa) tekrar iste.
                msg = llm_chat(sess.messages, TOOLS, no_think=True)
                sess.n_llm += 1
            content = strip_think(msg.get("content"))
            calls = msg.get("tool_calls") or []
            norm = []
            for i, c in enumerate(calls):
                fn = c.get("function", {})
                raw = fn.get("arguments") or "{}"
                norm.append({"id": c.get("id") or f"c{sess.n_llm}_{i}", "type": "function",
                             "function": {"name": fn.get("name"),
                                          "arguments": raw if isinstance(raw, str) else json.dumps(raw)}})
            if not norm:
                sess.result = {"cevap": content, "sql": ctx.last_sql or "", "varsayimlar": ""}
                sess.status = "done"
                return finish(sess, ctx)
            sess.messages.append({"role": "assistant", "content": content, "tool_calls": norm})
            state = _process_calls(sess, ctx, norm, on_step, auto_approve)
            if state in ("done", "waiting"):
                sess.status = state
                return finish(sess, ctx) if state == "done" else "waiting"
        sess.result = {"cevap": f"{MAX_STEPS} adım sınırına ulaşıldı; soru çözülemedi.", "sql": ctx.last_sql or "",
                       "varsayimlar": ""}
        sess.status = "done"
        return finish(sess, ctx)
    except Exception as e:
        sess.status, sess.error = "error", f"{type(e).__name__}: {e}"
        log(sess, ctx)
        return "error"


def resume(sess, ctx, answer, on_step=None, auto_approve=False):
    """Kullanıcının cevabını bekleyen araca verip döngüye kaldığı yerden devam eder."""
    p, sess.pending = sess.pending, None
    content = f"Kullanıcının cevabı: {answer}"
    if p["kind"] == "tablo_olustur":
        content = (ctx.tablo_olustur_calistir(p["prep"]) if str(answer).startswith("Onaylandı")
                   else f"Kullanıcı tablo oluşturmayı onaylamadı. Kullanıcının cevabı: {answer}")
    sess.messages.append({"role": "tool", "tool_call_id": p["call_id"], "content": content})
    sess.steps[-1]["cikti"] = f"Kullanıcı: {answer}" + (f"\n{content}" if p["kind"] == "tablo_olustur" else "")
    state = _process_calls(sess, ctx, p["rest"], on_step, auto_approve)
    if state == "done":
        sess.status = "done"
        return finish(sess, ctx)
    if state == "waiting":
        sess.status = "waiting"
        return "waiting"
    return run(sess, ctx, on_step, auto_approve)


def finish(sess, ctx):
    log(sess, ctx)
    return "done"


def log(sess, ctx):
    """Soru, adımlar ve SQL'ler loglanır; sonuç satırları (müşteri verisi) loglanmaz."""
    rec = {"zaman": dt.datetime.now().isoformat(timespec="seconds"), "soru": sess.question, "durum": sess.status,
           "hata": sess.error, "llm_adim": sess.n_llm, "sure_sn": round(time.time() - sess.started, 1),
           "adimlar": [{"arac": s["arac"], "girdi": short(s["girdi"], 2000)} for s in sess.steps],
           "sql": (sess.result or {}).get("sql"),
           "satir": None if ctx.last_df is None else len(ctx.last_df)}
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def save_verified(db, soru, sql, aciklama=""):
    tablolar = ", ".join(sorted(set(re.findall(r"\b([A-Z][A-Z0-9_$#]*\.[A-Z][A-Z0-9_$#]*)\b", (sql or "").upper()))))
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _DB_LOCK:
        db.execute("INSERT INTO verified_queries(soru, sql, aciklama, tablolar, created_at) VALUES(?,?,?,?,?)",
                   (soru, sql, aciklama, tablolar, now))
        db.execute("INSERT INTO feedback_log(soru, sql, sonuc, notlar, created_at) VALUES(?,?,?,?,?)",
                   (soru, sql, "dogru", aciklama, now))
        db.commit()


def save_feedback(db, soru, sql="", sonuc="yanlis", notlar=""):
    """Kullanıcının 👍/👎 geri bildirimini opsiyonel notuyla SQLite feedback_log tablosuna kaydeder."""
    with _DB_LOCK:
        db.execute(FEEDBACK_SQL)
        db.execute("INSERT INTO feedback_log(soru, sql, sonuc, notlar, created_at) VALUES(?,?,?,?,?)",
                   (soru, sql or "", sonuc, (notlar or "").strip(), dt.datetime.now().isoformat(timespec="seconds")))
        db.commit()


def list_feedback(db, sonuc=None, limit=50):
    with _DB_LOCK:
        db.execute(FEEDBACK_SQL)
    if sonuc:
        return pd.read_sql_query(
            "SELECT created_at AS tarih, sonuc, soru, notlar, sql FROM feedback_log WHERE sonuc=? ORDER BY id DESC LIMIT ?",
            db, params=(sonuc, limit))
    return pd.read_sql_query(
        "SELECT created_at AS tarih, sonuc, soru, notlar, sql FROM feedback_log ORDER BY id DESC LIMIT ?",
        db, params=(limit,))


def export_verified_excel(db):
    """verified_queries tablosunu ölçüm sekmesine doğrudan yüklenebilecek olcum_seti.xlsx formatında döner."""
    import io
    df = pd.read_sql_query(
        "SELECT soru, sql AS dogru_sql, COALESCE(aciklama, '') AS kategori FROM verified_queries ORDER BY id", db)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False)
    return buf.getvalue(), len(df)


def to_excel(df, soru="", sql=""):
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        (df if df is not None else pd.DataFrame()).to_excel(w, sheet_name="Sonuc", index=False)
        pd.DataFrame({"Bilgi": ["Soru", "SQL", "Tarih"],
                      "Deger": [soru, sql, dt.datetime.now().strftime("%Y-%m-%d %H:%M")]}).to_excel(
            w, sheet_name="Sorgu", index=False)
    return buf.getvalue()


# ------------------------------- Ölçüm -------------------------------
# Sonuç karşılaştırması (Databricks Genie benchmark ve BIRD yaklaşımına benzer):
#  - Satır sırası önemsiz; kolon adı ve sırası önemsiz (kolonlar içeriğe göre eşlenir).
#  - Doğru SQL'in her kolonu ajanın sonucunda varsa ve satırlar aynıysa DOĞRU (fazladan kolon serbest).
#  - Değilse hücre bazında F1 (BIRD soft-F1 benzeri) hesaplanır: >= 0.9 ise İNCELE, altıysa YANLIŞ.
#  - Ayrıca: eksik/fazla satır sayısı, tablo isabeti (doğru SQL'in tabloları ajanın SQL'inde var mı), adım ve süre.
EVAL_SQL = """CREATE TABLE IF NOT EXISTS eval_runs(run_id TEXT, zaman TEXT, kategori TEXT, soru TEXT, sonuc TEXT,
    f1 REAL, tablo_isabet REAL, eksik_satir INTEGER, fazla_satir INTEGER, adim INTEGER, sure_sn REAL,
    ajan_sql TEXT, notlar TEXT)"""
TABLE_RE = re.compile(r"\b([A-Z][A-Z0-9_$#]*)\s*\.\s*([A-Z][A-Z0-9_$#]*)\b")


def _nv(v):
    """Karşılaştırma için değer normalleştirme: sayılar 6 anlamlı basamak, boşluklar kırpılır."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, (int, float)) or type(v).__name__ == "Decimal":
        f = float(v)
        return str(int(f)) if f == int(f) and abs(f) < 1e15 else f"{f:.6g}"
    if hasattr(v, "isoformat"):
        return v.isoformat()[:19]
    return str(v).strip()


def compare_results(gold, pred):
    """(sonuc, f1, eksik_satir, fazla_satir, not) döner."""
    from collections import Counter
    g = gold.apply(lambda c: c.map(_nv))
    p = pred.apply(lambda c: c.map(_nv))
    used, mapping = set(), []
    for gc in g.columns:  # her doğru kolon için aynı değer kümesine sahip ajan kolonu
        gvals = sorted(g[gc])
        hit = next((pc for pc in p.columns if pc not in used and sorted(p[pc]) == gvals), None)
        if hit is None:
            break
        used.add(hit)
        mapping.append(hit)
    g_rows = Counter(map(tuple, g.values.tolist()))
    if len(mapping) == len(g.columns):
        p_rows = Counter(map(tuple, p[mapping].values.tolist()))
        if p_rows == g_rows:
            extra_cols = len(p.columns) - len(g.columns)
            return "DOĞRU", 1.0, 0, 0, (f"{extra_cols} fazladan kolon" if extra_cols else "aynı sonuç")
    gc_ = Counter(v for row in g.values.tolist() for v in row)
    pc_ = Counter(v for row in p.values.tolist() for v in row)
    inter = sum((gc_ & pc_).values())
    prec = inter / max(sum(pc_.values()), 1)
    rec = inter / max(sum(gc_.values()), 1)
    f1 = 0.0 if inter == 0 else 2 * prec * rec / (prec + rec)
    eksik, fazla = max(len(g) - len(p), 0), max(len(p) - len(g), 0)
    note = (f"satır {len(p)}/{len(g)}, kolon {len(p.columns)}/{len(g.columns)}; "
            f"hücre örtüşmesi P={prec:.2f} R={rec:.2f}")
    return ("İNCELE" if f1 >= 0.9 else "YANLIŞ"), round(f1, 3), eksik, fazla, note


def table_hit(db, gold_sql, pred_sql):
    """Doğru SQL'deki tabloların kaçı ajanın SQL'inde de var (sadece katalogdaki gerçek tablolar sayılır)."""
    def tables(sql):
        found = {f"{a}.{b}" for a, b in TABLE_RE.findall(re.sub(r"'(?:''|[^'])*'", "''", (sql or "").upper()))}
        return {t for t in found if db.execute("SELECT 1 FROM objects WHERE owner=? AND name=?",
                                               tuple(t.split(".", 1))).fetchone()}
    gt, pt = tables(gold_sql), tables(pred_sql)
    return round(len(gt & pt) / len(gt), 2) if gt else None


def evaluate(db, conn, rows, on_progress=None, eval_max_rows=EVAL_MAX_ROWS, use_verified=False):
    """rows: [(soru, dogru_sql, kategori)]. Sonuçlar eval_runs tablosuna da kaydedilir (sürümleri karşılaştırmak için).

    Hem doğru SQL hem ajanın SQL'i aynı satır üst sınırıyla (eval_max_rows) karşılaştırılır; varsayılan olarak
    use_verified=False ile ölçüm sırasında onaylı sorgu sızıntısı (data leakage) engellenir.
    """
    with _DB_LOCK:
        db.execute(EVAL_SQL)
        db.commit()
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M")
    results = []
    for i, row in enumerate(rows, 1):
        soru, gold, kategori = (list(row) + [""])[:3]
        ctx, sess = Ctx(db, conn, use_verified=use_verified), Session(soru)
        state = run(sess, ctx, auto_approve=True)
        pred_sql = (sess.result or {}).get("sql", "")
        rec = {"kategori": kategori or "", "soru": soru, "sonuc": "HATA", "f1": 0.0, "tablo_isabet": None,
               "eksik_satir": None, "fazla_satir": None, "adim": sess.n_llm,
               "sure_sn": round(time.time() - sess.started, 1), "ajan_sql": pred_sql, "notlar": ""}
        try:
            gdf, g_more = fetch_df(conn, check_select(gold), eval_max_rows)
            rec["tablo_isabet"] = table_hit(db, gold, pred_sql)
            if state != "done" or ctx.last_df is None:
                rec["notlar"] = sess.error or "ajan sorgu çalıştırmadı"
            else:
                pdf, p_more = ctx.last_df, False
                if ctx.last_more and ctx.last_sql and eval_max_rows > SHOW_ROWS:
                    pdf, p_more = fetch_df(conn, check_select(ctx.last_sql), eval_max_rows)
                (rec["sonuc"], rec["f1"], rec["eksik_satir"], rec["fazla_satir"],
                 rec["notlar"]) = compare_results(gdf, pdf)
                if g_more or p_more:
                    rec["notlar"] += f" (uyarı: >{eval_max_rows:,} satırda kesildi)"
        except Exception as e:
            rec["notlar"] = f"doğru SQL çalışmadı: {str(e)[:150]}"
        results.append(rec)
        with _DB_LOCK:
            db.execute("INSERT INTO eval_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, dt.datetime.now().isoformat(timespec="seconds"), rec["kategori"], soru, rec["sonuc"],
                        rec["f1"], rec["tablo_isabet"], rec["eksik_satir"], rec["fazla_satir"], rec["adim"],
                        rec["sure_sn"], pred_sql, rec["notlar"]))
            db.commit()
        if on_progress:
            on_progress(i, len(rows), rec)
    return run_id, pd.DataFrame(results)


def eval_history(db):
    with _DB_LOCK:
        db.execute(EVAL_SQL)
    return pd.read_sql_query("""SELECT run_id AS olcum, COUNT(*) AS soru, SUM(sonuc='DOĞRU') AS dogru,
                                SUM(sonuc='İNCELE') AS incele, SUM(sonuc='YANLIŞ') AS yanlis, SUM(sonuc='HATA') AS hata,
                                ROUND(AVG(f1),3) AS ort_f1, ROUND(AVG(tablo_isabet),2) AS tablo_isabet,
                                ROUND(AVG(sure_sn),0) AS ort_sure_sn
                                FROM eval_runs GROUP BY run_id ORDER BY run_id DESC""", db)
