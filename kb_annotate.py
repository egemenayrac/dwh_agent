# -*- coding: utf-8 -*-
"""
kb_annotate.py - Bilgi tabanına LLM ile kart yazdırır (Faz 2)

  1. DOSYA KARTLARI  SQL içeren her dosya (script) için: ne üretir, hangi adımlarla, hangi tablolardan,
                     parametreleri, iş kuralları, üretim mi deneme mi. Dosyanın TAMAMI okunur (yorumlar dahil).
  2. TABLO KARTLARI  Sorgularda kullanılan her tablo için: ne tutar, satır seviyesi, anahtar ve tarih kolonları,
                     önemli kolonların anlamı, kısaltmalar, nasıl join edilir, tuzaklar.
  3. SÖZLÜK          Kartlardaki kısaltmalar toplanır (ekip onayı için).

LLM'e sadece metadata ve SQL gider, tablo verisi (müşteri satırları) gitmez.
Kaldığı yerden devam eder; girdisi değişmeyen kart tekrar yazılmaz. Gece çalıştırmak için uygundur.

Çıktılar (KB_DIR altında):
  kb.sqlite içinde file_cards, table_cards, glossary tabloları
  kartlar/dosyalar/*.md, kartlar/tablolar/*.md   (insanların okuyacağı dokümantasyon)
  kart_ozet_tablolar.csv, kart_ozet_dosyalar.csv, sozluk.csv   (Excel'de gözden geçirmek için)

Kurulum: kb_builder.py ile aynı klasörde olmalı (onun ayarlarını ve fonksiyonlarını kullanır).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import openai
from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_builder as kb  # noqa: E402  (KB_DIR, dosya okuma ve SQL çıkarma fonksiyonları)
import schemas  # noqa: E402

# =============================== AYARLAR ===============================
LLM_URL = kb.LLM_URL             # ayarlar.py'den gelir
LLM_API_KEY = ""                 # boş bırak: IBTHINK_API_KEY ortam değişkeni veya ibthink_anahtar.txt okunur
LLM_MODEL = kb.LLM_MODEL
LLM_VERIFY_SSL = "windows"
LLM_TIMEOUT = 600
LLM_MAX_TOKENS = 4096        # IBThink üst sınırı 4096 (düşünme + cevap birlikte)
LLM_RETRIES = 5              # Geçici ağ / 429 / 5xx hatalarında üst sınır
NO_THINK = True              # Qwen'in uzun "düşünme"sini kapatır; token bütçesi JSON'a kalır
LLM_WORKERS = 1              # IBThink kişisel anahtar paralel limiti 1; uygulama anahtarı alınırsa artırılabilir

DO_FILES = True
DO_TABLES = True
MAX_HOURS = 10               # bu kadar saat sonra durur (kaldığı yerden devam eder)
LIMIT_FILES = 0              # 0 = hepsi; deneme için ör. 5
LIMIT_TABLES = 0             # 0 = hepsi; deneme için ör. 10

MAX_FILE_CHARS = 40_000      # dosya metninin LLM'e giden kısmı (baş + son)
MAX_COLUMNS = 250            # tablo kartında gösterilecek en fazla kolon
MAX_EXAMPLE_SQL = 4          # tablo kartı için örnek sorgu sayısı
PROMPT_VERSION = "v3"        # prompt değişince artır: kartlar yeniden yazılır
ONAYLI_BILGILER = "onayli_bilgiler.txt"   # KB_DIR içinde; ekibin kesinleştirdiği bilgiler (her satır bir bilgi)
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ibthink_anahtar.txt")
# ======================================================================

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------- LLM -------------------------------
def resolve_ssl_verify(setting):
    if setting is False or setting != "windows":
        return setting
    if not hasattr(ssl, "enum_certificates"):
        return True
    pems = {ssl.DER_cert_to_PEM_cert(cert) for store in ("ROOT", "CA")
            for cert, encoding, _ in ssl.enum_certificates(store) if encoding == "x509_asn"}
    path = os.path.join(tempfile.gettempdir(), "kb_annotate_windows_ca.pem")
    with open(path, "w", encoding="ascii") as f:
        f.write("".join(sorted(pems)))
    return path


VERIFY = resolve_ssl_verify(LLM_VERIFY_SSL)
FACTS = ""
_HTTP_LOCAL = threading.local()


def build_httpx_verify(verify_val=None):
    """httpx 0.28+ için SSL doğrulama nesnesi (bool veya ssl.SSLContext) üretir."""
    v = VERIFY if verify_val is None else verify_val
    if v == "windows":
        v = resolve_ssl_verify("windows")
    if isinstance(v, bool) or isinstance(v, ssl.SSLContext):
        return v
    if isinstance(v, str) and v:
        return ssl.create_default_context(cafile=v)
    return True


def openai_base_url(url):
    """Tam /v1/chat/completions adresini OpenAI SDK base_url biçimine (/v1) indirger."""
    clean = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if clean.endswith(suffix):
            clean = clean[:-len(suffix)]
            break
    return clean or "https://localhost/v1"


def _is_ssl_error(exc):
    cur = exc
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False


def _is_retryable_llm_error(exc):
    if _is_ssl_error(exc):
        return False
    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.InternalServerError,
                        openai.APIConnectionError, httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", 0) in (429, 500, 502, 503, 504)
    return False


class _WaitWithRetryAfter(wait_exponential_jitter):
    """429 yanıtında Retry-After başlığına uyar; yoksa exponential backoff + jitter uygular."""

    def __call__(self, retry_state):
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        resp = getattr(exc, "response", None)
        if resp is not None:
            ra = (resp.headers.get("retry-after") or resp.headers.get("Retry-After") or "").strip()
            if ra.isdigit():
                return min(float(int(ra)), 120.0)
        return super().__call__(retry_state)


def _http():
    """Her iş parçacığı için ayrı kalıcı httpx.Client döner (thread-safe Keep-Alive, Windows SSL uyumlu)."""
    verify_cfg = build_httpx_verify(VERIFY)
    client = getattr(_HTTP_LOCAL, "http_client", None)
    cfg_sig = (VERIFY, LLM_TIMEOUT)
    if client is None or getattr(_HTTP_LOCAL, "http_sig", None) != cfg_sig:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        client = httpx.Client(verify=verify_cfg, timeout=httpx.Timeout(LLM_TIMEOUT, connect=30.0))
        _HTTP_LOCAL.http_client = client
        _HTTP_LOCAL.http_sig = cfg_sig
        _HTTP_LOCAL.openai_client = None
    return client


def _openai_client(key):
    """Her iş parçacığı için OpenAI SDK istemcisi (retry tenacity ile yönetilir)."""
    base = openai_base_url(LLM_URL)
    sig = (base, key, VERIFY, LLM_TIMEOUT)
    client = getattr(_HTTP_LOCAL, "openai_client", None)
    if client is None or getattr(_HTTP_LOCAL, "openai_sig", None) != sig:
        client = openai.OpenAI(
            base_url=base,
            api_key=key or "not-required",
            http_client=_http(),
            max_retries=0,
            timeout=LLM_TIMEOUT,
        )
        _HTTP_LOCAL.openai_client = client
        _HTTP_LOCAL.openai_sig = sig
    return client


def get_api_key():
    """Anahtar sırası: koddaki LLM_API_KEY, IBTHINK_API_KEY ortam değişkeni, ibthink_anahtar.txt dosyası."""
    raw = LLM_API_KEY or os.getenv("IBTHINK_API_KEY") or ""
    if not raw and os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            raw = f.read().strip()
    return re.sub(r"^bearer\s+", "", raw.strip(), flags=re.I)


def parse_json(text):
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    text = re.sub(r"(?s)^.*?</think>", "", text)
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"cevapta JSON yok: {text[:120]!r}")
    return json.loads(text[start:end + 1])


def ask_llm(system, user, response_model=None):
    """JSON sözlük döndürür (openai SDK + httpx + tenacity); bozuk JSON/şemada düzeltme ister."""
    key = get_api_key()
    if NO_THINK:
        user += "\n\n/no_think"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    client = _openai_client(key)
    retrying = Retrying(
        stop=stop_after_attempt(max(1, LLM_RETRIES)),
        wait=_WaitWithRetryAfter(multiplier=2.0, max=60.0, jitter=3.0),
        retry=retry_if_exception(_is_retryable_llm_error),
        reraise=True,
    )
    last = ""
    for _ in range(3):
        kwargs = {
            "model": LLM_MODEL if LLM_MODEL else openai.NOT_GIVEN,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": LLM_MAX_TOKENS,
            "stream": False,
        }
        try:
            resp = retrying(lambda: client.chat.completions.create(**kwargs))
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            code = getattr(exc, "status_code", 401)
            raise RuntimeError(f"HTTP {code}: IBThink anahtarı reddedildi.") from exc
        except openai.APIStatusError as exc:
            raise RuntimeError(f"HTTP {getattr(exc, 'status_code', '?')}: {str(exc)[:300]}") from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise RuntimeError(f'SSL hatası (LLM_VERIFY_SSL="windows" kontrol edin): {str(exc)[:250]}') from exc
            raise RuntimeError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc

        choices = getattr(resp, "choices", None) or []
        choice = choices[0] if choices else None
        content = (getattr(getattr(choice, "message", None), "content", None) or "") if choice else ""
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        if finish_reason == "length" and "</think>" not in content and "<think>" in content:
            last = "cevap düşünme aşamasında token sınırına takıldı"
            continue
        try:
            parsed = parse_json(content)
            if response_model is not None:
                parsed = response_model.model_validate(parsed).model_dump()
            return parsed
        except (ValueError, json.JSONDecodeError, ValidationError) as e:
            last = f"JSON okunamadı ({e}); finish_reason={finish_reason}"
            messages = messages[:2] + [{"role": "assistant", "content": content[-4000:]},
                                       {"role": "user", "content": "Cevabın geçerli JSON değildi. İstenen şemada, "
                                                                   "SADECE tek bir JSON nesnesi döndür."}]
    raise RuntimeError(last or "LLM cevap vermedi")


# ----------------------------- Veritabanı -----------------------------
CARD_SQL = """
CREATE TABLE IF NOT EXISTS file_cards(path TEXT PRIMARY KEY, input_hash TEXT, card TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS table_cards(owner TEXT, table_name TEXT, input_hash TEXT, card TEXT, status TEXT,
    created_at TEXT, PRIMARY KEY(owner, table_name));
CREATE TABLE IF NOT EXISTS glossary(term TEXT PRIMARY KEY, expansion TEXT, n INTEGER, examples TEXT);
"""


VARSAYILAN_BILGILER = """# Ekibin onayladığı bilgiler. Her satır bir bilgi; # ile başlayan satırlar yok sayılır.
# Bu dosya değişince kartlar yeniden yazılır. LLM bunları kesin bilgi olarak kullanır.
PART_ID: Partition (dönem) kolonudur, YYYYMM biçiminde sayıdır (ör. 202910). Müşteri ya da karşı taraf numarası DEĞİLDİR.
"""


def onayli_bilgiler():
    path = os.path.join(kb.KB_DIR, ONAYLI_BILGILER)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(VARSAYILAN_BILGILER)
    lines = [l.strip() for l in kb.read_text(path).splitlines()]
    facts = [l for l in lines if l and not l.startswith("#")]
    return ("\n\nEKİBİN ONAYLADIĞI BİLGİLER (bunlar kesindir, kanıtla çelişse bile bunlara uy):\n"
            + "\n".join(f"- {x}" for x in facts)) if facts else ""


def h(*parts):
    return hashlib.sha1((PROMPT_VERSION + "\x00" + "\x00".join(map(str, parts))).encode("utf-8")).hexdigest()


def short(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + " …"


# ============================ 1. DOSYA KARTLARI ============================
FILE_SYSTEM = """Sen bir bankanın kredi riski ekibinde çalışan kıdemli bir veri analistisin. Ekibin ortak klasöründeki
bir scripti okuyup ekip için dokümantasyon kartı yazıyorsun. Sadece verilen metne dayan; emin olmadığın yerde
"belirsiz" yaz, uydurma. Tablo ve kolon adlarını olduğu gibi (BÜYÜK harf) yaz. Türkçe yaz.

SADECE şu şemada tek bir JSON nesnesi döndür:
{
  "amac": "scriptin ne ürettiği, 1-3 cümle",
  "is_alani": "ör. IFRS 9 / ECL, tahsilat, gecikme takibi, PD modeli, kartlar, KOBİ, raporlama",
  "cikti": "nihai çıktı: hangi tablo / rapor / Excel, satır seviyesi",
  "adimlar": [{"sira": 1, "ne_yapar": "...", "olusturdugu_tablo": "... veya null", "kullandigi_tablolar": ["..."]}],
  "girdi_tablolari": ["OWNER.TABLO", "..."],
  "parametreler": [{"ad": "ör. donem", "aciklama": "...", "ornek": "202509"}],
  "is_kurallari": ["ör. temerrüt = 90+ gün gecikme", "..."],
  "donemsel": true,
  "durum": "uretim | deneme | eski | belirsiz",
  "durum_gerekcesi": "neden bu durum",
  "tekrar_kullanilabilir": true,
  "anahtar_kelimeler": ["..."]
}"""


def file_text_for_llm(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            import zipfile
            with zipfile.ZipFile(path) as z:
                sheets = re.findall(r'<sheet [^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "replace"))
            sqls = kb.sql_from_excel(path)
            text = f"[Excel] Sayfalar: {', '.join(sheets)}\n\nİçindeki sorgular:\n" + "\n;\n".join(sqls)
        elif ext == ".ipynb":
            cells = json.loads(kb.read_text(path)).get("cells", [])
            text = "\n\n".join(f"# [{c.get('cell_type')}]\n" + "".join(c.get("source", [])) for c in cells)
        else:
            text = kb.read_text(path)
    except Exception as e:
        return f"(dosya okunamadı: {type(e).__name__})"
    text = kb.redact(text)
    if len(text) > MAX_FILE_CHARS:
        head = int(MAX_FILE_CHARS * 0.75)
        text = text[:head] + "\n\n…[ORTASI KESİLDİ]…\n\n" + text[-(MAX_FILE_CHARS - head):]
    return text


def file_jobs(db):
    rows = db.execute("""SELECT f.path, f.mtime FROM files f WHERE f.n_snippets > 0 AND f.status = 'ok'
                         ORDER BY f.mtime DESC""").fetchall()
    jobs = []
    for path, mtime in rows:
        tables = db.execute("""
            SELECT DISTINCT st.owner, st.table_name, st.role, o.comments
            FROM snippet_sources ss JOIN snippet_tables st ON st.snippet_id = ss.snippet_id
            LEFT JOIN objects o ON o.owner = st.owner AND o.name = st.table_name
            WHERE ss.source = ? ORDER BY st.role, st.owner, st.table_name""", (path,)).fetchall()
        info = "\n".join(f"- {o}.{t} ({'okunur' if r == 'read' else 'oluşturulur/yazılır'})"
                         + (f": {short(c, 120)}" if c else "") for o, t, r, c in tables[:80])
        jobs.append((path, mtime, info))
    return jobs


def run_files(db, deadline):
    jobs = file_jobs(db)
    todo = []
    for path, mtime, info in jobs:
        key = h(path, mtime, info, FACTS)
        old = db.execute("SELECT input_hash, status FROM file_cards WHERE path=?", (path,)).fetchone()
        if old and old[0] == key and old[1] == "ok":
            continue
        todo.append((path, mtime, info, key))
    if LIMIT_FILES:
        todo = todo[:LIMIT_FILES]
    print(f"Dosya kartı: {len(jobs)} dosya, {len(todo)} tanesi yazılacak")

    def work(item):
        path, mtime, info, key = item
        user = (f"Dosya: {path}\nSon değişiklik: {mtime}\n\nBu dosyanın kullandığı/oluşturduğu tablolar "
                f"(katalog açıklamalarıyla):\n{info or '-'}\n\n=== DOSYA İÇERİĞİ ===\n{file_text_for_llm(path)}")
        return ask_llm(FILE_SYSTEM + FACTS, user, response_model=schemas.FileCardSchema)

    _run(db, todo, work, lambda item, card, status: db.execute(
        "INSERT OR REPLACE INTO file_cards VALUES(?,?,?,?,?)",
        (item[0], item[3], json.dumps(card, ensure_ascii=False), status, kb.now())), deadline, "dosya")


# ============================ 2. TABLO KARTLARI ============================
TABLE_SYSTEM = """Sen bir bankanın veri ambarını dokümante eden kıdemli bir veri mimarısın. Bir tablonun metadata'sı,
kullanıldığı örnek sorgular, join ilişkileri ve kökeni veriliyor. Bunlardan ekip için tablo kartı yazıyorsun.
Sadece verilen kanıta dayan; tahmin ettiğin yerleri "muhtemelen" diye belirt, uydurma. Kolon adlarını olduğu gibi yaz.
Kanıtı okurken:
- distinct ve nulls Oracle istatistikleridir. distinct değeri satır sayısına yakın kolon büyük ihtimalle anahtardır.
- aralık=... kolonun en küçük ve en büyük değeridir. 19000101-20991231 arasındaki sayılar YYYYMMDD tarih,
  190001-209912 arası YYYYMM dönemdir; bunu tarih_kolonlari'nda belirt.
- degerler=... kod kolonunun değerleri ve oranlarıdır; anlamını tahmin edebiliyorsan onemli_kolonlar'da yaz.
- "EKİBİN KULLANDIĞI JOIN'LER" sorgulardan doğrulanmıştır ve karta otomatik eklenir; onları TEKRAR YAZMA.
  aday_joinler'e sadece doğrulanmış listede OLMAYAN, kanıta dayanan olası join'leri yaz (ör. ORTAK KOLON ADLARI'ndan).
- PART_ID, AS_OF_DT gibi dönem/tarih kolonlarıyla kurulan eşitlikler zaman hizalamasıdır, asıl anahtar değildir.
- degerler=[8=Aktif %85 ...] biçimindeyse kodların anlamı LOOKUP tablosundan gelmiştir ve kesindir.
- Satır seviyesini anahtar kolonlar ve tarih kolonundan çıkar (ör. CASH_LOAN_ID + AS_OF_DT -> kredi x gün).
Türkçe yaz.

SADECE şu şemada tek bir JSON nesnesi döndür:
{
  "ozet": "tablo ne tutar, 1-3 cümle",
  "is_alani": "ör. IFRS 9 / ECL, tahsilat, gecikme, müşteri, kartlar, teminat, referans/kod tablosu",
  "satir_seviyesi": "bir satır neyi temsil eder, ör. kredi x gün, müşteri x ay",
  "anahtar_kolonlar": ["..."],
  "tarih_kolonlari": [{"kolon": "...", "anlam": "ör. snapshot tarihi, sayısal YYYYMMDD"}],
  "onemli_kolonlar": [{"kolon": "...", "anlam": "..."}],
  "kisaltmalar": {"DLQ": "delinquency (gecikme)"},
  "aday_joinler": ["ör. HIST.X.CUST_ID = EDW.CUST.CUST_ID (neden: iki tabloda aynı isimli kolon, 12 sorguda birlikte)"],
  "tuzaklar": ["ör. tarihçe tablosu: tek gün için AS_OF_DT filtresi şart"],
  "kisisel_tablo_notu": "kişisel şemadaysa: ne için üretilmiş, kaynağı ne; değilse null"
}"""


def table_jobs(db):
    has_profile = db.execute("SELECT 1 FROM sqlite_master WHERE name='col_profile'").fetchone() is not None
    has_lookup = db.execute("SELECT 1 FROM sqlite_master WHERE name='lookup_values'").fetchone() is not None
    if not has_profile:
        print("  (kb_profile.py çalıştırılmamış: kartlarda kod değerleri ve tarih aralıkları olmayacak)")
    rows = db.execute("""
        SELECT o.owner, o.name, o.type, o.comments, o.num_rows, o.last_ddl, o.mod_time, o.personal,
               COALESCE(u.n_files, 0), u.last_used
        FROM v_usage u JOIN objects o ON o.owner = u.owner AND o.name = u.table_name
        ORDER BY u.n_files DESC, u.n_queries DESC""").fetchall()
    jobs = []
    for owner, name, otype, cmt, nrows, last_ddl, mod_time, personal, n_files, last_used in rows:
        cols = db.execute("""SELECT column_name, data_type, nullable, num_distinct, num_nulls, comments FROM columns
                             WHERE owner=? AND table_name=? ORDER BY column_id""", (owner, name)).fetchall()
        prof = {}
        if has_profile:
            prof = {c: (lo, hi, tv) for c, lo, hi, tv in db.execute(
                "SELECT column_name, low, high, top_values FROM col_profile WHERE owner=? AND table_name=?",
                (owner, name))}

        lk = {}
        if has_lookup:
            for col, code, descr in db.execute(
                    "SELECT column_name, code, descr FROM lookup_values WHERE owner=? AND table_name=?", (owner, name)):
                lk.setdefault(col, {})[code] = descr

        def decode_values(col, tv):
            if not tv or col not in lk:
                return tv
            out = []
            for part in tv.split(", "):
                code, _, pct = part.rpartition(" ")
                out.append(f"{code}={short(lk[col].get(code), 40)} {pct}" if code in lk[col] else part)
            return ", ".join(out)

        def col_line(c, t, nl, nd, nn, cm):
            lo, hi, tv = prof.get(c, (None, None, None))
            tv = decode_values(c, tv)
            return (f"{c} {t} {'NULL' if nl == 'Y' else 'NOT NULL'} distinct={nd} nulls={nn}"
                    + (f" aralık={lo}..{hi}" if lo or hi else "") + (f" degerler=[{tv}]" if tv else "")
                    + (f" -- {short(cm, 150)}" if cm else ""))
        col_txt = "\n".join(col_line(*r) for r in cols[:MAX_COLUMNS])
        if len(cols) > MAX_COLUMNS:
            col_txt += f"\n… ve {len(cols) - MAX_COLUMNS} kolon daha"
        keys = db.execute("SELECT type, cols, ref_table FROM keys WHERE owner=? AND table_name=?",
                          (owner, name)).fetchall()
        joins = db.execute("""SELECT owner1||'.'||table1||'.'||col1, owner2||'.'||table2||'.'||col2, n_files, evidence
                              FROM join_edges WHERE (owner1=? AND table1=?) OR (owner2=? AND table2=?)
                              ORDER BY n_files DESC LIMIT 20""", (owner, name, owner, name)).fetchall()
        my_cols = {r[0] for r in cols}
        co_txt = []
        for o2, t2, n in db.execute("""
                SELECT st2.owner, st2.table_name, COUNT(DISTINCT st.snippet_id) FROM snippet_tables st
                JOIN snippet_tables st2 ON st2.snippet_id = st.snippet_id
                     AND NOT (st2.owner = st.owner AND st2.table_name = st.table_name)
                WHERE st.owner=? AND st.table_name=? GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 12""", (owner, name)):
            common = sorted(my_cols & {r[0] for r in db.execute(
                "SELECT column_name FROM columns WHERE owner=? AND table_name=?", (o2, t2))})
            if common:
                co_txt.append(f"- {o2}.{t2} ({n} sorguda birlikte): {', '.join(common[:8])}")
        up = db.execute("""SELECT DISTINCT source_owner||'.'||source_table FROM v_lineage
                           WHERE target_owner=? AND target_table=? LIMIT 15""", (owner, name)).fetchall()
        down = db.execute("""SELECT DISTINCT target_owner||'.'||target_table FROM v_lineage
                             WHERE source_owner=? AND source_table=? LIMIT 15""", (owner, name)).fetchall()
        examples = db.execute("""
            SELECT s.text, COUNT(DISTINCT ss.source) AS n FROM snippet_tables st
            JOIN snippets s ON s.id = st.snippet_id JOIN snippet_sources ss ON ss.snippet_id = s.id
            WHERE st.owner=? AND st.table_name=? AND st.role='read' AND LENGTH(s.text) < 6000
            GROUP BY s.id ORDER BY n DESC, LENGTH(s.text) LIMIT ?""", (owner, name, MAX_EXAMPLE_SQL)).fetchall()
        uses = db.execute("""
            SELECT DISTINCT fc.card FROM snippet_tables st JOIN snippet_sources ss ON ss.snippet_id = st.snippet_id
            JOIN file_cards fc ON fc.path = ss.source AND fc.status = 'ok'
            WHERE st.owner=? AND st.table_name=? LIMIT 5""", (owner, name)).fetchall()
        use_txt = "\n".join("- " + short(json.loads(c).get("amac", ""), 200) for (c,) in uses)
        fam = kb.family_of(name)
        evidence = (
            f"Tablo: {owner}.{name} ({otype}){' — KİŞİSEL ŞEMA' if personal else ''}\n"
            f"Katalog açıklaması: {cmt or '-'}\nSatır sayısı (istatistik): {nrows}\n"
            f"Son DDL: {last_ddl} | Son veri değişikliği: {mod_time or '-'} | Son kullanıldığı dosya: {last_used}\n"
            f"Kullanan dosya sayısı: {n_files}\n"
            + (f"Dönemsel tablo ailesi: {fam}\n" if fam else "")
            + f"\nKOLONLAR:\n{col_txt}\n"
            + (f"\nTANIMLI ANAHTARLAR: {keys}\n" if keys else "")
            + ("\nEKİBİN KULLANDIĞI JOIN'LER (sorgulardan doğrulanmış; dosya sayısı, kanıt):\n"
               + "\n".join(f"{a} = {b} ({n} dosya, {ev})" for a, b, n, ev in joins) + "\n" if joins else "")
            + ("\nBİRLİKTE KULLANILAN TABLOLAR VE ORTAK KOLON ADLARI (aday join anahtarları, doğrulanmamış):\n"
               + "\n".join(co_txt) + "\n" if co_txt else "")
            + ("\nBU TABLO ŞUNLARDAN ÜRETİLİYOR: " + ", ".join(r[0] for r in up) + "\n" if up else "")
            + ("\nBU TABLODAN ÜRETİLENLER: " + ", ".join(r[0] for r in down) + "\n" if down else "")
            + ("\nBU TABLOYU KULLANAN SCRİPTLERİN AMAÇLARI:\n" + use_txt + "\n" if use_txt else "")
            + "\nÖRNEK SORGULAR:\n" + "\n\n---\n".join(short(t, 2500) for t, _ in examples)
        )
        jobs.append((owner, name, evidence))
    return jobs


def run_tables(db, deadline):
    jobs = table_jobs(db)
    todo = []
    for owner, name, evidence in jobs:
        key = h(owner, name, evidence, FACTS)
        old = db.execute("SELECT input_hash, status FROM table_cards WHERE owner=? AND table_name=?",
                         (owner, name)).fetchone()
        if old and old[0] == key and old[1] == "ok":
            continue
        todo.append((owner, name, evidence, key))
    if LIMIT_TABLES:
        todo = todo[:LIMIT_TABLES]
    print(f"Tablo kartı: {len(jobs)} tablo, {len(todo)} tanesi yazılacak")
    _run(db, todo, lambda item: ask_llm(TABLE_SYSTEM + FACTS, item[2], response_model=schemas.TableCardSchema),
         lambda item, card, status: db.execute(
        "INSERT OR REPLACE INTO table_cards VALUES(?,?,?,?,?,?)",
        (item[0], item[1], item[3], json.dumps(card, ensure_ascii=False), status, kb.now())), deadline, "tablo")


# ------------------------------ ortak döngü ------------------------------
def _run(db, todo, work, save, deadline, label):
    done = errors = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
        pending = {}
        items = iter(todo)
        for _ in range(LLM_WORKERS * 2):
            item = next(items, None)
            if item:
                pending[pool.submit(work, item)] = item
        while pending:
            for fut in as_completed(list(pending)):
                item = pending.pop(fut)
                try:
                    save(item, fut.result(), "ok")
                    done += 1
                except Exception as e:
                    save(item, {"hata": str(e)[:300]}, "error")
                    errors += 1
                db.commit()
                n = done + errors
                if n % 10 == 0 or n == len(todo):
                    rate = (time.time() - t0) / max(n, 1)
                    print(f"  {label}: {n}/{len(todo)} ({errors} hata) — kalan tahmini "
                          f"{rate * (len(todo) - n) / 60:.0f} dk")
                if time.time() < deadline:
                    nxt = next(items, None)
                    if nxt:
                        pending[pool.submit(work, nxt)] = nxt
                break
    if time.time() >= deadline:
        print(f"  Süre doldu (MAX_HOURS={MAX_HOURS}); tekrar çalıştırınca kaldığı yerden devam eder.")
    print(f"  {label} kartı: {done} yazıldı, {errors} hata")


# ============================== 3. ÇIKTILAR ==============================
def safe_name(s):
    return re.sub(r"[^\w.\-]+", "_", s)[:150]


def as_list(x):
    return x if isinstance(x, list) else ([] if x in (None, "") else [x])


def table_joins_md(db, owner, name):
    """Doğrulanmış join'ler veritabanından: karşı tablo başına koşullar birlikte, en çok kullanılan üstte."""
    personal = re.compile(kb.PERSONAL_REGEX)
    groups = {}
    for o1, t1, c1, o2, t2, c2, n_files, ev in db.execute(
            """SELECT owner1, table1, col1, owner2, table2, col2, n_files, evidence FROM join_edges
               WHERE (owner1=? AND table1=?) OR (owner2=? AND table2=?)""", (owner, name, owner, name)):
        if (o1, t1) != (owner, name):
            o1, t1, c1, o2, t2, c2 = o2, t2, c2, o1, t1, c1
        g = groups.setdefault((o2, t2), {"conds": [], "n": 0, "ev": set()})
        g["conds"].append(f"{c1} = {t2}.{c2}")
        g["n"] = max(g["n"], n_files or 0)
        g["ev"].add(ev)
    core, pers = [], []
    for (o2, t2), g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        line = (f"- `{o2}.{t2}`: `" + " AND ".join(sorted(set(g["conds"]))) + f"` — {g['n']} dosya"
                + (" (ara tablo üzerinden)" if g["ev"] == {"ara_tablo_uzerinden"} else ""))
        (pers if personal.match(o2) else core).append(line)
    md = "## Nasıl join edilir (sorgulardan doğrulanmış)\n" + ("\n".join(core) or "- (doğrulanmış join yok)") + "\n"
    if pers:
        md += "\n**Kişisel şemalardaki tablolarla:**\n" + "\n".join(pers[:10]) + "\n"
    return md, len(groups)


def evidence_level(db, owner, name, n_join_tables):
    """Kartın dayandığı kanıtın gücü (modelin kendi tahmini değil)."""
    cmt, = db.execute("SELECT comments FROM objects WHERE owner=? AND name=?", (owner, name)).fetchone() or (None,)
    n_cols, n_ccmt = db.execute("SELECT COUNT(*), SUM(comments IS NOT NULL) FROM columns WHERE owner=? AND table_name=?",
                                (owner, name)).fetchone()
    n_prof = 0
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='col_profile'").fetchone():
        n_prof, = db.execute("SELECT COUNT(*) FROM col_profile WHERE owner=? AND table_name=?", (owner, name)).fetchone()
    n_files = (db.execute("SELECT n_files FROM v_usage WHERE owner=? AND table_name=?", (owner, name)).fetchone()
               or (0,))[0]
    checks = [(bool(cmt), "tablo açıklaması var", "tablo açıklaması yok"),
              ((n_ccmt or 0) >= 0.3 * max(n_cols or 1, 1), "kolon açıklamaları var", "kolon açıklamaları az"),
              (n_prof >= 0.5 * max(n_cols or 1, 1), "kolon profilleri var", "kolon profili az"),
              (n_join_tables >= 1, f"{n_join_tables} tabloyla doğrulanmış join", "doğrulanmış join yok"),
              ((n_files or 0) >= 5, f"{n_files} dosyada kullanılıyor", f"sadece {n_files} dosyada kullanılıyor")]
    score = sum(ok for ok, _, _ in checks)
    level = "yüksek" if score >= 4 else "orta" if score >= 2 else "düşük"
    return level, "; ".join(good if ok else bad for ok, good, bad in checks)


def export(db):
    import csv
    base = os.path.join(kb.KB_DIR, "kartlar")
    os.makedirs(os.path.join(base, "tablolar"), exist_ok=True)
    os.makedirs(os.path.join(base, "dosyalar"), exist_ok=True)
    note = "> Otomatik üretildi (LLM), henüz doğrulanmadı.\n\n"

    trows, gloss = [], {}
    for owner, name, card in db.execute("SELECT owner, table_name, card FROM table_cards WHERE status='ok'").fetchall():
        c = json.loads(card)
        joins_md, n_join_tables = table_joins_md(db, owner, name)
        level, why = evidence_level(db, owner, name, n_join_tables)
        aday = as_list(c.get("aday_joinler"))
        md = [f"# {owner}.{name}\n", note, f"**Özet:** {c.get('ozet', '')}\n",
              f"**İş alanı:** {c.get('is_alani', '')}  \n**Satır seviyesi:** {c.get('satir_seviyesi', '')}  \n"
              f"**Kanıt düzeyi:** {level} ({why})\n",
              "## Anahtar kolonlar\n" + "\n".join(f"- `{k}`" for k in as_list(c.get("anahtar_kolonlar"))) + "\n",
              "## Tarih kolonları\n" + "\n".join(f"- `{d.get('kolon')}`: {d.get('anlam')}" for d in
                                                 as_list(c.get("tarih_kolonlari")) if isinstance(d, dict)) + "\n",
              "## Önemli kolonlar\n" + "\n".join(f"- `{d.get('kolon')}`: {d.get('anlam')}" for d in
                                                 as_list(c.get("onemli_kolonlar")) if isinstance(d, dict)) + "\n",
              joins_md,
              ("## Aday join'ler (doğrulanmamış, model önerisi)\n" + "\n".join(f"- {j}" for j in aday) + "\n")
              if aday else "",
              "## Tuzaklar\n" + "\n".join(f"- {t}" for t in as_list(c.get("tuzaklar"))) + "\n"]
        if c.get("kisisel_tablo_notu"):
            md.append(f"## Kişisel tablo notu\n{c['kisisel_tablo_notu']}\n")
        with open(os.path.join(base, "tablolar", safe_name(f"{owner}.{name}") + ".md"), "w", encoding="utf-8") as f:
            f.write("\n".join(x for x in md if x))
        trows.append([owner, name, c.get("is_alani"), c.get("satir_seviyesi"), c.get("ozet"), level, ""])
        for term, exp in (c.get("kisaltmalar") or {}).items() if isinstance(c.get("kisaltmalar"), dict) else []:
            g = gloss.setdefault(str(term).upper(), {})
            g.setdefault(str(exp), []).append(f"{owner}.{name}")

    frows = []
    for path, card in db.execute("SELECT path, card FROM file_cards WHERE status='ok'"):
        c = json.loads(card)
        steps = "\n".join(f"{s.get('sira')}. {s.get('ne_yapar')}"
                          + (f" → `{s.get('olusturdugu_tablo')}`" if s.get("olusturdugu_tablo") else "")
                          for s in as_list(c.get("adimlar")) if isinstance(s, dict))
        md = [f"# {os.path.basename(path)}\n", f"`{path}`\n", note, f"**Amaç:** {c.get('amac', '')}\n",
              f"**İş alanı:** {c.get('is_alani', '')}  \n**Çıktı:** {c.get('cikti', '')}  \n"
              f"**Durum:** {c.get('durum', '')} — {c.get('durum_gerekcesi', '')}\n",
              "## Adımlar\n" + steps + "\n",
              "## Girdi tabloları\n" + "\n".join(f"- `{t}`" for t in as_list(c.get("girdi_tablolari"))) + "\n",
              "## Parametreler\n" + "\n".join(f"- `{p.get('ad')}`: {p.get('aciklama')} (ör. {p.get('ornek')})"
                                               for p in as_list(c.get("parametreler")) if isinstance(p, dict)) + "\n",
              "## İş kuralları\n" + "\n".join(f"- {k}" for k in as_list(c.get("is_kurallari"))) + "\n"]
        with open(os.path.join(base, "dosyalar", safe_name(os.path.basename(path)) + "_"
                               + hashlib.sha1(path.encode()).hexdigest()[:6] + ".md"), "w", encoding="utf-8") as f:
            f.write("\n".join(md))
        frows.append([path, c.get("is_alani"), c.get("durum"), c.get("amac"), c.get("cikti"), ""])

    db.execute("DELETE FROM glossary")
    for term, exps in gloss.items():
        best = max(exps.items(), key=lambda kv: len(kv[1]))
        db.execute("INSERT INTO glossary VALUES(?,?,?,?)",
                   (term, best[0], sum(len(v) for v in exps.values()), ", ".join(best[1][:5])))
    db.commit()

    def write_csv(name, header, rows):
        with open(os.path.join(kb.KB_DIR, name), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(rows)

    write_csv("kart_ozet_tablolar.csv", ["sema", "tablo", "is_alani", "satir_seviyesi", "ozet", "kanit_duzeyi", "onay"],
              trows)
    write_csv("kart_ozet_dosyalar.csv", ["dosya", "is_alani", "durum", "amac", "cikti", "onay"], frows)
    write_csv("sozluk.csv", ["kisaltma", "acilim", "gecis_sayisi", "ornek_tablolar", "onay"],
              [list(r) + [""] for r in db.execute("SELECT * FROM glossary ORDER BY n DESC")])
    print(f"\nYazıldı: {base}\\tablolar ({len(trows)} kart), {base}\\dosyalar ({len(frows)} kart)\n"
          f"         kart_ozet_tablolar.csv, kart_ozet_dosyalar.csv, sozluk.csv ({len(gloss)} kısaltma)")


def main():
    db = kb.open_kb()
    db.executescript(CARD_SQL)
    global FACTS
    FACTS = onayli_bilgiler()
    deadline = time.time() + MAX_HOURS * 3600
    t0 = time.time()
    if DO_FILES:
        print("1/3 Dosya kartları")
        run_files(db, deadline)
    if DO_TABLES and time.time() < deadline:
        print("\n2/3 Tablo kartları")
        run_tables(db, deadline)
    print("\n3/3 Markdown, CSV ve sözlük")
    export(db)
    print(f"Toplam süre: {(time.time() - t0) / 60:.0f} dk ({dt.datetime.now():%H:%M})")
    db.close()


if __name__ == "__main__":
    main()
