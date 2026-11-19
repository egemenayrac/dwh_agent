# -*- coding: utf-8 -*-
"""
kb_builder.py - Veri ambarı bilgi tabanı oluşturucu (Faz 1: LLM kullanmaz, tamamen deterministik)

Ne yapar:
  1. METADATA  Erişebildiğin TÜM şemalardaki tablo/view'ların kolonlarını, açıklamalarını, satır sayısını,
               oluşturma / son DDL / son istatistik / son DML zamanını, anahtarlarını toplar.
  2. DB KODU   View ve materialized view tanımlarını, PL/SQL paketlerini (ETL kodu) ve erişim varsa
               V$SQL'deki sorguları okur -> hangi tablo hangi tablodan üretiliyor (köken / lineage).
  3. DOSYALAR  Verdiğin klasörleri (ortak alan dahil) gezer; .sql .py .ipynb .txt .sas .r .xlsx .xlsm ...
               içindeki SQL'leri çıkarır (Excel'de bağlantı sorguları, Power Query ve hücrelere yapıştırılmış
               sorgular dahil). Şifre benzeri değerler maskelenir.
  4. RAPOR     SQL'lerde geçen tabloları kataloğa eşler, kullanım sayısı ve son kullanım tarihini hesaplar,
               tablo envanteri ve köken CSV'lerini yazar.

Çıktı: kb.sqlite (ajanın kullanacağı bilgi tabanı), tablo_envanteri.csv, koken.csv
Her adım kaldığı yerden devam eder: yarıda kesersen tekrar çalıştır, bitmiş şema/dosyaları atlar.

Kurulum: pip install oracledb   (başka paket gerekmez)
"""
import ast
import base64
import csv
import datetime as dt
import getpass
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

import oracledb

# =============================== AYARLAR ===============================
# Kuruma özel değerler (kullanıcı, sunucu, klasör, LLM adresi) burada DEĞİL, aynı klasördeki ayarlar.py
# dosyasında durur. ayarlar_ornek.py'yi ayarlar.py adıyla kopyalayıp doldur. Böylece kod dosyalarını
# güncellediğinde kendi ayarların silinmez (ve ayarlar.py GitHub'a gitmez).
DB_USER = ""
DB_HOST, DB_PORT, DB_SERVICE = "", 1521, ""
KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")   # bilgi tabanı (yerel disk önerilir)
CRAWL_ROOTS = []                   # taranacak klasörler: ortak alan (UNC yolu da olur), kendi klasörlerin ...
LLM_URL = ""                       # OpenAI uyumlu /v1/chat/completions adresi
LLM_MODEL = ""
API_KEY_PAGE = ""                  # API anahtarının alındığı sayfa (hata mesajında gösterilir)

# Her çalıştırmada kendisi karar verir: Oracle bilgileri bu kadar günden eskiyse yeniler (yoksa Oracle'a hiç
# bağlanmaz, şifre de sormaz); dosyalardan sadece yeni / değişmiş olanları okur; raporu her seferinde yeniler.
META_MAX_AGE_DAYS = 7
FORCE_REFRESH = False    # True: her şeyi baştan topla (normalde gerekmez)

OWNER_SKIP_REGEX = r"^$"           # atlanacak şemalar, ör. r"TEST$|^HMN_"
PERSONAL_REGEX = r"^[ASTU]\d{3,}"  # kişisel şema kalıbı (ör. A12345, T67890)
PLSQL_TYPES = ("PACKAGE BODY", "PROCEDURE", "FUNCTION", "TRIGGER")

FILE_EXTS = {".sql", ".txt", ".py", ".ipynb", ".sas", ".r", ".bas", ".vbs", ".pls", ".pck", ".pkb",
             ".prc", ".fnc", ".xlsx", ".xlsm", ".log"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".ipynb_checkpoints", "$RECYCLE.BIN", "venv", ".venv"}
MAX_FILE_MB = 30
MAX_SNIPPET_CHARS = 100_000
DICT_TIMEOUT_MS = 30 * 60_000      # sözlük sorguları için zaman aşımı (şema başına)
# ======================================================================

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from ayarlar import *  # noqa: F401,F403  (kuruma özel değerler; yukarıdakilerin üzerine yazar)
except ImportError:
    print("UYARI: ayarlar.py bulunamadı. ayarlar_ornek.py'yi ayarlar.py adıyla kopyalayıp doldur.")
DB_DSN = oracledb.makedsn(DB_HOST, DB_PORT, service_name=DB_SERVICE) if DB_HOST else ""

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
oracledb.defaults.fetch_lobs = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objects(owner TEXT, name TEXT, type TEXT, created TEXT, last_ddl TEXT, status TEXT,
    num_rows INTEGER, last_analyzed TEXT, partitioned TEXT, temporary TEXT, comments TEXT,
    mod_inserts INTEGER, mod_updates INTEGER, mod_deletes INTEGER, mod_time TEXT, truncated TEXT,
    personal INTEGER, PRIMARY KEY(owner, name));
CREATE TABLE IF NOT EXISTS columns(owner TEXT, table_name TEXT, column_id INTEGER, column_name TEXT,
    data_type TEXT, nullable TEXT, num_distinct INTEGER, num_nulls INTEGER, comments TEXT,
    PRIMARY KEY(owner, table_name, column_name));
CREATE TABLE IF NOT EXISTS keys(owner TEXT, table_name TEXT, type TEXT, cols TEXT, ref_table TEXT);
CREATE TABLE IF NOT EXISTS synonyms(owner TEXT, name TEXT, table_owner TEXT, table_name TEXT);
CREATE TABLE IF NOT EXISTS harvest_log(owner TEXT, step TEXT, done_at TEXT, PRIMARY KEY(owner, step));
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime TEXT, size INTEGER, n_snippets INTEGER,
    status TEXT, scanned_at TEXT);
CREATE TABLE IF NOT EXISTS snippets(id INTEGER PRIMARY KEY, hash TEXT UNIQUE, text TEXT, kind TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS snippet_sources(snippet_id INTEGER, source TEXT, source_kind TEXT, source_time TEXT,
    executions INTEGER, pos INTEGER, PRIMARY KEY(snippet_id, source));
CREATE TABLE IF NOT EXISTS snippet_refs(snippet_id INTEGER, qual TEXT, name TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS snippet_tables(snippet_id INTEGER, owner TEXT, table_name TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS snippet_joins(snippet_id INTEGER, q1 TEXT, t1 TEXT, c1 TEXT, q2 TEXT, t2 TEXT, c2 TEXT);
CREATE TABLE IF NOT EXISTS families(owner TEXT, family TEXT, n_tables INTEGER, first_table TEXT, last_table TEXT,
    PRIMARY KEY(owner, family));
CREATE TABLE IF NOT EXISTS snippet_families(snippet_id INTEGER, owner TEXT, family TEXT, role TEXT);
CREATE INDEX IF NOT EXISTS ix_cols ON columns(owner, table_name);
CREATE INDEX IF NOT EXISTS ix_refs ON snippet_refs(snippet_id);
CREATE INDEX IF NOT EXISTS ix_src ON snippet_sources(source);
CREATE INDEX IF NOT EXISTS ix_st ON snippet_tables(owner, table_name);
CREATE INDEX IF NOT EXISTS ix_st2 ON snippet_tables(snippet_id);
CREATE VIEW IF NOT EXISTS v_usage AS
SELECT st.owner, st.table_name,
       COUNT(DISTINCT st.snippet_id) AS n_queries,
       COUNT(DISTINCT CASE WHEN ss.source_kind = 'file' THEN ss.source END) AS n_files,
       COUNT(DISTINCT CASE WHEN ss.source_kind IN ('view', 'plsql') THEN ss.source END) AS n_db_objects,
       SUM(CASE WHEN ss.source_kind = 'vsql' THEN ss.executions ELSE 0 END) AS vsql_executions,
       MAX(CASE WHEN ss.source_kind IN ('file', 'vsql') THEN ss.source_time END) AS last_used
FROM snippet_tables st JOIN snippet_sources ss ON ss.snippet_id = st.snippet_id
WHERE st.role = 'read'
GROUP BY st.owner, st.table_name;
CREATE VIEW IF NOT EXISTS v_lineage AS
SELECT DISTINCT w.owner AS target_owner, w.table_name AS target_table,
       r.owner AS source_owner, r.table_name AS source_table, s.kind
FROM snippet_tables w JOIN snippet_tables r ON r.snippet_id = w.snippet_id AND r.role = 'read'
JOIN snippets s ON s.id = w.snippet_id
WHERE w.role = 'write' AND NOT (w.owner = r.owner AND w.table_name = r.table_name);
"""


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def ts(v):
    return v.isoformat(timespec="seconds") if isinstance(v, (dt.datetime, dt.date)) else v


def open_kb():
    os.makedirs(KB_DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(KB_DIR, "kb.sqlite"))
    db.executescript(SCHEMA_SQL)
    if "pos" not in [r[1] for r in db.execute("PRAGMA table_info(snippet_sources)")]:
        db.execute("ALTER TABLE snippet_sources ADD COLUMN pos INTEGER")  # eski sürümden kalan bilgi tabanı
    return db


def ask_password():
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(f"{DB_USER} şifresi: ")
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    pwd = simpledialog.askstring("Oracle", f"{DB_USER} şifresi:", show="*", parent=root)
    root.destroy()
    if not pwd:
        raise SystemExit("Şifre girilmedi.")
    return pwd


def connect():
    conn = oracledb.connect(user=DB_USER, password=ask_password(), dsn=DB_DSN)
    conn.call_timeout = DICT_TIMEOUT_MS
    print(f"Oracle bağlandı (sürüm {conn.version})")
    return conn


def fetch(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.arraysize = 5000
        cur.prefetchrows = 5000
        cur.execute(sql, params or {})
        return cur.fetchall()


def done(db, owner, step):
    """Bu adım META_MAX_AGE_DAYS gün içinde yapıldıysa True."""
    if FORCE_REFRESH:
        return False
    cutoff = (dt.datetime.now() - dt.timedelta(days=META_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    row = db.execute("SELECT done_at FROM harvest_log WHERE owner=? AND step=?", (owner, step)).fetchone()
    return row is not None and row[0] >= cutoff


def mark(db, owner, step):
    db.execute("INSERT OR REPLACE INTO harvest_log VALUES(?,?,?)", (owner, step, now()))
    db.commit()


def list_owners(conn):
    rows = fetch(conn, "SELECT username FROM all_users WHERE oracle_maintained = 'N' ORDER BY username")
    skip = re.compile(OWNER_SKIP_REGEX)
    owners = [r[0] for r in rows if not (OWNER_SKIP_REGEX not in ("", "^$") and skip.search(r[0]))]
    personal = re.compile(PERSONAL_REGEX)
    return sorted(owners, key=lambda o: (bool(personal.match(o)), o))  # önce ortak şemalar


# =========================== 1. METADATA ===========================
def harvest_metadata(conn, db, owners):
    personal = re.compile(PERSONAL_REGEX)
    t0, errors = time.time(), 0
    for i, o in enumerate(owners, 1):
        if done(db, o, "meta"):
            continue
        p = {"o": o}
        try:
            objs = fetch(conn, """SELECT object_name, object_type, created, last_ddl_time, status FROM all_objects
                                  WHERE owner = :o AND object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')
                                    AND object_name NOT LIKE 'BIN$%'""", p)
            if not objs:
                mark(db, o, "meta")
                continue
            tabs = {r[0]: r[1:] for r in fetch(conn, """SELECT table_name, num_rows, last_analyzed, partitioned,
                                                              temporary FROM all_tables WHERE owner = :o""", p)}
            cmts = dict(fetch(conn, """SELECT table_name, comments FROM all_tab_comments
                                       WHERE owner = :o AND comments IS NOT NULL""", p))
            try:
                mods = {r[0]: r[1:] for r in fetch(conn, """SELECT table_name, inserts, updates, deletes, timestamp,
                    truncated FROM all_tab_modifications WHERE table_owner = :o AND partition_name IS NULL""", p)}
            except oracledb.DatabaseError:
                mods = {}
            cols = fetch(conn, """SELECT table_name, column_id, column_name, data_type ||
                    CASE WHEN data_type IN ('VARCHAR2','CHAR','NVARCHAR2','NCHAR') THEN '(' || char_length || ')'
                         WHEN data_type = 'NUMBER' AND data_precision IS NOT NULL
                              THEN '(' || data_precision || ',' || data_scale || ')' END,
                    nullable, num_distinct, num_nulls FROM all_tab_columns WHERE owner = :o""", p)
            ccmts = {(r[0], r[1]): r[2] for r in fetch(conn, """SELECT table_name, column_name, comments
                     FROM all_col_comments WHERE owner = :o AND comments IS NOT NULL""", p)}
            keys = fetch(conn, """
                SELECT con.table_name, con.constraint_type,
                       LISTAGG(cc.column_name, ',') WITHIN GROUP (ORDER BY cc.position),
                       MAX(rc.owner || '.' || rc.table_name)
                FROM all_constraints con
                JOIN all_cons_columns cc ON cc.owner = con.owner AND cc.constraint_name = con.constraint_name
                LEFT JOIN all_constraints rc ON rc.owner = con.r_owner AND rc.constraint_name = con.r_constraint_name
                WHERE con.owner = :o AND con.constraint_type IN ('P', 'R', 'U')
                GROUP BY con.table_name, con.constraint_name, con.constraint_type""", p)
        except oracledb.DatabaseError as e:
            print(f"  [{i}/{len(owners)}] {o}: HATA {str(e)[:150]} (atlandı, sonraki çalışmada tekrar denenir)")
            errors += 1
            continue

        db.execute("DELETE FROM objects WHERE owner=?", (o,))
        db.execute("DELETE FROM columns WHERE owner=?", (o,))
        db.execute("DELETE FROM keys WHERE owner=?", (o,))
        is_personal = int(bool(personal.match(o)))
        rows = []
        for name, otype, created, last_ddl, status in objs:
            t = tabs.get(name, (None, None, None, None))
            m = mods.get(name, (None, None, None, None, None))
            rows.append((o, name, otype, ts(created), ts(last_ddl), status, t[0], ts(t[1]), t[2], t[3],
                         cmts.get(name), m[0], m[1], m[2], ts(m[3]), m[4], is_personal))
        db.executemany("INSERT OR REPLACE INTO objects VALUES(" + ",".join("?" * 17) + ")", rows)
        db.executemany("INSERT OR REPLACE INTO columns VALUES(?,?,?,?,?,?,?,?,?)",
                       [(o, r[0], r[1], r[2], r[3], r[4], r[5], r[6], ccmts.get((r[0], r[2]))) for r in cols])
        db.executemany("INSERT INTO keys VALUES(?,?,?,?,?)", [(o, *r) for r in keys])
        mark(db, o, "meta")
        print(f"  [{i}/{len(owners)}] {o}: {len(objs)} nesne, {len(cols)} kolon ({(time.time() - t0) / 60:.1f} dk)")

    syn = fetch(conn, """SELECT owner, synonym_name, table_owner, table_name FROM all_synonyms
                         WHERE db_link IS NULL AND table_owner IS NOT NULL
                           AND table_owner NOT IN ('SYS', 'SYSTEM')""")
    db.execute("DELETE FROM synonyms")
    db.executemany("INSERT INTO synonyms VALUES(?,?,?,?)", syn)
    db.commit()
    print(f"  {len(syn)} synonym")
    return errors


# ======================== SQL çıkarma / analiz ========================
SQL_HINT = re.compile(r"(?is)\bselect\b.+?\bfrom\b|\bcreate\s+(?:or\s+replace\s+)?(?:global\s+temporary\s+)?table\b"
                      r"|\binsert\s+(?:/\*.*?\*/\s*)?into\b|\bmerge\s+into\b")
SQL_START = re.compile(r"(?is)\b(select|with|create\s+(?:or\s+replace\s+)?(?:force\s+)?(?:global\s+temporary\s+)?"
                       r"(?:table|view|materialized\s+view)|insert\s+(?:/\*.*?\*/\s*)?(?:all\s+)?into|merge\s+into)\b")
SECRETS = [
    (re.compile(r'(?i)((?:password|passwd|pwd|parola|sifre|şifre|secret|api_?key|token)\w*["\']?\s*[=:]\s*)'
                r'(["\']?)[^"\'\s,;)]+'), r"\1\2***"),
    (re.compile(r"(?i)\b(identified\s+by\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)\b([A-Z0-9_]+)/[^\s/@\"']+@"), r"\1/***@"),
]
IDENT = r'"?([A-Z][A-Z0-9_$#]*)"?'
NAME_RE = re.compile(IDENT + r"(?:\s*\.\s*" + IDENT + r")?")
WRITE_RE = re.compile(r"\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?(?:GLOBAL\s+TEMPORARY\s+)?"
                      r"(TABLE|VIEW|MATERIALIZED\s+VIEW)|(INSERT)\s+(?:ALL\s+)?INTO|(MERGE)\s+INTO)\s+"
                      r"(" + IDENT + r"(?:\s*\.\s*" + IDENT + r")?)")
FROM_RE = re.compile(r"\b(?:FROM|JOIN|USING)\b(.*?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bUNION\b|\bMINUS\b"
                     r"|\bINTERSECT\b|\bCONNECT\b|\bSTART\s+WITH\b|\bON\b|\bJOIN\b|\bLEFT\b|\bRIGHT\b|\bINNER\b"
                     r"|\bFULL\b|\bCROSS\b|\bFETCH\b|\bFROM\b|\bSELECT\b|\bPIVOT\b|\bWHEN\b|[()]|;|$)", re.S)
CTE_RE = re.compile(r"(?:\bWITH\b|,)\s*([A-Z][A-Z0-9_$#]*)\s*(?:\([^()]*\))?\s+AS\s*\(")
JOIN_EQ_RE = re.compile(r"(?<![.\w])([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)\s*(?:\(\+\))?\s*=\s*"
                        r"([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)(?![.\w])")
ALIAS_RE = re.compile(r"\s*(?:AS\s+)?([A-Z][A-Z0-9_$#]*)")
NOT_ALIAS = {"WHERE", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "FULL", "CROSS", "GROUP", "ORDER", "UNION", "MINUS",
             "PARTITION", "SAMPLE", "USING", "CONNECT", "START", "PIVOT", "UNPIVOT", "FETCH", "FOR", "NATURAL"}
NOT_TABLES = {"DUAL", "TABLE", "LATERAL", "THE", "ONLY", "SELECT", "WITH", "VALUES", "XMLTABLE", "JSON_TABLE"}


DATE_SUFFIX_RE = re.compile(r"(?<=_)((?:19|20)\d{2})(0[1-9]|1[0-2])((?:0[1-9]|[12]\d|3[01])?)(?=_|$)")


def family_of(name):
    """Dönemsel tablo ailesi: ECL_RESULTS_202504, ECL_RESULTS_20250430 ve dinamik 'ECL_RESULTS_' || donem
    -> ECL_RESULTS_{DONEM}. Dönem eki yoksa None."""
    f = DATE_SUFFIX_RE.sub("{DONEM}", name)
    if f.endswith("_"):
        f += "{DONEM}"
    return f if f != name else None


def redact(text):
    for pat, rep in SECRETS:
        text = pat.sub(rep, text)
    return text


def clean_sql(s):
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"'(?:''|[^'])*'", "''", s)
    return s.upper()


def _analyze_regex(sql):
    """Regex tabanlı SQL analizi (sqlglot parse edemediğinde yedek olarak çalışır)."""
    s = clean_sql(sql)
    refs, target, kind = set(), None, "select"
    ctes = set(CTE_RE.findall(s))  # WITH ad AS (...) -> tablo değil
    m = WRITE_RE.search(s)
    if m:
        what = m.group(1) or m.group(2) or m.group(3)
        kind = {"TABLE": "ctas" if re.search(r"\bAS\b\s*\(?\s*(SELECT|WITH)\b", s) else "ddl", "INSERT": "insert",
                "MERGE": "merge"}.get(what, "view")
        q, n = (m.group(5), m.group(6)) if m.group(6) else (None, m.group(5))
        target = f"{q}.{n}" if q else n
        refs.add((q, n, "write"))
    aliases = {}
    for clause in FROM_RE.findall(s):
        for item in clause.split(","):
            item = item.strip()
            nm = NAME_RE.match(item)
            if not nm:
                continue
            q, n = (nm.group(1), nm.group(2)) if nm.group(2) else (None, nm.group(1))
            if n in NOT_TABLES or q == "X_PARAM" or (q is None and n in ctes):
                continue
            if (q, n, "write") not in refs:
                refs.add((q, n, "read"))
            am = ALIAS_RE.match(item[nm.end():])
            names = [n] + ([am.group(1)] if am and am.group(1) not in NOT_ALIAS else [])
            for a in names:
                aliases[a] = (q, n) if aliases.get(a, (q, n)) == (q, n) else None  # çakışan alias -> belirsiz
    joins = set()
    for a1, c1, a2, c2 in JOIN_EQ_RE.findall(s):
        t1, t2 = aliases.get(a1), aliases.get(a2)
        if t1 and t2 and t1 != t2:
            e1, e2 = (t1[0], t1[1], c1), (t2[0], t2[1], c2)
            if (e1[1], e1[2]) > (e2[1], e2[2]):
                e1, e2 = e2, e1
            joins.add(e1 + e2)
    return kind, target, refs, joins


def _analyze_sqlglot(sql):
    """sqlglot (Oracle diyalekti) ile AST tabanlı tablo, hedef ve join çıkarımı; başarısızsa None döner."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return None
    try:
        tree = sqlglot.parse_one(sql, read="oracle")
    except Exception:
        return None
    if tree is None:
        return None

    refs, target, kind = set(), None, "select"
    ctes = {(cte.alias or "").upper() for cte in tree.find_all(exp.CTE) if cte.alias}

    def _tbl_pair(tbl):
        name = (tbl.name or "").strip('"').upper().split("@")[0]   # db link eki (TABLO@LINK) atılır
        qual = (tbl.db or "").strip('"').upper() or None
        return qual, name

    write_tbl = None
    if isinstance(tree, exp.Create):
        what = (tree.args.get("kind") or "").upper()
        kind = ("view" if "VIEW" in what else
                ("ctas" if tree.args.get("expression") is not None else "ddl"))
        write_tbl = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    elif isinstance(tree, exp.Insert):
        kind = "insert"
        write_tbl = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
    elif isinstance(tree, exp.Merge):
        kind = "merge"
        write_tbl = tree.this

    write_key = None
    if isinstance(write_tbl, exp.Table):
        wq, wn = _tbl_pair(write_tbl)
        if wn and wn not in NOT_TABLES:
            target = f"{wq}.{wn}" if wq else wn
            write_key = (wq, wn)
            refs.add((wq, wn, "write"))

    aliases = {}
    for tbl in tree.find_all(exp.Table):
        if tbl is write_tbl:
            continue
        q, n = _tbl_pair(tbl)
        if not n or n in NOT_TABLES or q == "X_PARAM" or (q is None and n in ctes):
            continue
        if (q, n) != write_key:
            refs.add((q, n, "read"))
        alias = (tbl.alias or "").strip('"').upper()
        for a in ([n] + ([alias] if alias and alias != n else [])):
            aliases[a] = (q, n) if aliases.get(a, (q, n)) == (q, n) else None

    joins = set()
    for eq in tree.find_all(exp.EQ):
        if isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column):
            a1, c1 = (eq.left.table or "").strip('"').upper(), (eq.left.name or "").strip('"').upper()
            a2, c2 = (eq.right.table or "").strip('"').upper(), (eq.right.name or "").strip('"').upper()
            if not (a1 and c1 and a2 and c2):
                continue
            t1, t2 = aliases.get(a1), aliases.get(a2)
            if t1 and t2 and t1 != t2:
                e1, e2 = (t1[0], t1[1], c1), (t2[0], t2[1], c2)
                if (e1[1], e1[2]) > (e2[1], e2[2]):
                    e1, e2 = e2, e1
                joins.add(e1 + e2)

    if not refs and not target:
        return None
    return kind, target, refs, joins


def analyze(sql):
    """(kind, target, refs, joins) - refs: {(qual|None, name, role)}, joins: {(q1, t1, c1, q2, t2, c2)}"""
    res = _analyze_sqlglot(sql)
    if res is not None:
        return res
    return _analyze_regex(sql)


def sql_from_text(text):
    out = []
    for part in re.split(r";|\n\s*/\s*(?:\n|$)", text):
        m = SQL_START.search(part)
        if not m:
            continue
        s = part[m.start():].strip()
        if len(s) >= 25 and SQL_HINT.search(s):
            out.append(s)
    return out


def sql_from_python(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return sql_from_text(code)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
        elif isinstance(node, ast.JoinedStr):
            s = "".join(v.value if isinstance(v, ast.Constant) else "X_PARAM" for v in node.values)
        else:
            continue
        if SQL_HINT.search(s):
            out += sql_from_text(s)
    return out


def sql_from_notebook(raw):
    out = []
    for cell in json.loads(raw).get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source", []))
        if src.lstrip().lower().startswith("%%sql"):
            out += sql_from_text(src)
        else:
            out += sql_from_python("\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("%", "!"))))
    return out


def _mashup_m(b64):
    data = base64.b64decode(b64.strip())
    n = int.from_bytes(data[4:8], "little")
    with zipfile.ZipFile(io.BytesIO(data[8:8 + n])) as z:
        texts = [z.read(f).decode("utf-8", "replace") for f in z.namelist() if f.endswith(".m")]
    return [t.replace("#(lf)", "\n").replace("#(cr)", "").replace("#(tab)", " ").replace('""', '"') for t in texts]


def sql_from_excel(path):
    out = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "xl/connections.xml" in names:
            x = z.read("xl/connections.xml").decode("utf-8", "replace")
            for cmd in re.findall(r'command="([^"]*)"', x):
                out += sql_from_text(html.unescape(cmd))
        if "xl/sharedStrings.xml" in names:
            x = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for t in re.findall(r"<t[^>]*>(.*?)</t>", x, re.S):
                t = html.unescape(t)
                if SQL_HINT.search(t):
                    out += sql_from_text(t)
        for n in names:
            if n.startswith("customXml/item") and n.endswith(".xml"):
                raw = z.read(n)
                x = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else \
                    raw.decode("utf-8", "replace")
                m = re.search(r"<DataMashup[^>]*>(.*?)</DataMashup>", x, re.S)
                if m:
                    try:
                        for t in _mashup_m(m.group(1)):
                            out += sql_from_text(t)
                    except Exception:
                        pass
    return out


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def extract_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return sql_from_excel(path)
    text = read_text(path)
    if ext == ".py":
        return sql_from_python(text)
    if ext == ".ipynb":
        return sql_from_notebook(text)
    return sql_from_text(text)


def add_snippets(db, texts, source, source_kind, source_time, executions=None):
    n = 0
    for pos, t in enumerate(texts):
        t = redact(t.strip())[:MAX_SNIPPET_CHARS]
        kind, target, refs, _ = analyze(t)
        if not refs:
            continue
        h = hashlib.sha1(re.sub(r"\s+", " ", t.upper()).encode("utf-8")).hexdigest()
        row = db.execute("SELECT id FROM snippets WHERE hash=?", (h,)).fetchone()
        if row:
            sid = row[0]
        else:
            sid = db.execute("INSERT INTO snippets(hash, text, kind, target) VALUES(?,?,?,?)",
                             (h, t, kind, target)).lastrowid
            db.executemany("INSERT INTO snippet_refs VALUES(?,?,?,?)", [(sid, q, nm, r) for q, nm, r in refs])
        db.execute("INSERT OR IGNORE INTO snippet_sources VALUES(?,?,?,?,?,?)",
                   (sid, source, source_kind, source_time, executions, pos))
        n += 1
    return n


# =========================== 2. DB KODU ===========================
def harvest_db_code(conn, db, owners):
    t0, errors = time.time(), 0
    for i, o in enumerate(owners, 1):
        if done(db, o, "code"):
            continue
        p = {"o": o}
        try:
            ddl = {r[0]: ts(r[1]) for r in fetch(conn, """SELECT object_name, last_ddl_time FROM all_objects
                   WHERE owner = :o AND object_type IN ('VIEW','MATERIALIZED VIEW','PACKAGE BODY','PROCEDURE',
                   'FUNCTION','TRIGGER')""", p)}
            if not ddl:
                mark(db, o, "code")
                continue
            views = fetch(conn, "SELECT view_name, text FROM all_views WHERE owner = :o", p)
            mviews = fetch(conn, "SELECT mview_name, query FROM all_mviews WHERE owner = :o", p)
            src = fetch(conn, f"""SELECT name, type, text FROM all_source WHERE owner = :o
                                  AND type IN ({",".join("'" + t + "'" for t in PLSQL_TYPES)})
                                  ORDER BY name, type, line""", p)
        except oracledb.DatabaseError as e:
            print(f"  [{i}/{len(owners)}] {o}: HATA {str(e)[:150]}")
            errors += 1
            continue
        db.execute("DELETE FROM snippet_sources WHERE source LIKE ? AND source_kind IN ('view','plsql')",
                   (f"%:{o}.%",))
        n = 0
        for name, text in views:
            n += add_snippets(db, [f'CREATE VIEW "{o}"."{name}" AS {text or ""}'], f"view:{o}.{name}", "view",
                              ddl.get(name))
        for name, text in mviews:
            n += add_snippets(db, [f'CREATE MATERIALIZED VIEW "{o}"."{name}" AS {text or ""}'],
                              f"view:{o}.{name}", "view", ddl.get(name))
        code = {}
        for name, typ, line in src:
            code.setdefault((name, typ), []).append(line or "")
        for (name, typ), lines in code.items():
            n += add_snippets(db, sql_from_text("".join(lines)), f"plsql:{o}.{name}", "plsql", ddl.get(name))
        mark(db, o, "code")
        if views or mviews or code:
            print(f"  [{i}/{len(owners)}] {o}: {len(views) + len(mviews)} view, {len(code)} PL/SQL, {n} sorgu "
                  f"({(time.time() - t0) / 60:.1f} dk)")

    if not done(db, "*", "vsql"):
        try:
            rows = fetch(conn, """SELECT sql_id, executions, last_active_time, sql_fulltext FROM v$sql
                                  WHERE command_type IN (1, 2, 3, 189)
                                    AND parsing_schema_name NOT IN ('SYS', 'SYSTEM')""")
            n = sum(add_snippets(db, [txt or ""], f"vsql:{sid}", "vsql", ts(last), ex) for sid, ex, last, txt in rows)
            print(f"  V$SQL: {len(rows)} sorgu okundu, {n} kayıt")
        except oracledb.DatabaseError as e:
            print(f"  V$SQL okunamadı (yetki yok, normal): {str(e)[:80]}")
        mark(db, "*", "vsql")
    return errors


# =========================== 3. DOSYALAR ===========================
def crawl_files(db):
    max_bytes = MAX_FILE_MB * 1024 * 1024
    seen = n_new = n_sql = n_err = 0
    t0 = time.time()
    # Eski sürümde sorgu sırası kaydedilmeden okunan dosyalar otomatik olarak yeniden okunur
    no_order = {r[0] for r in db.execute("SELECT DISTINCT source FROM snippet_sources "
                                         "WHERE source_kind = 'file' AND pos IS NULL")}
    for root in CRAWL_ROOTS:
        if not os.path.isdir(root):
            print(f"  !!! Klasör bulunamadı ya da erişim yok: {root}  -> AYARLAR'daki CRAWL_ROOTS'u kontrol et")
            continue
        print(f"  Taranıyor: {root}")
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("~")]
            for fn in filenames:
                if fn.startswith("~$") or os.path.splitext(fn)[1].lower() not in FILE_EXTS:
                    continue
                path = os.path.join(dirpath, fn)
                seen += 1
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                mtime = dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                old = db.execute("SELECT mtime, size, status FROM files WHERE path=?", (path,)).fetchone()
                if (not FORCE_REFRESH and old and old[0] == mtime and old[1] == st.st_size and old[2] == "ok"
                        and path not in no_order):
                    continue
                if st.st_size > max_bytes:
                    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                               (path, mtime, st.st_size, 0, "too_big", now()))
                    continue
                db.execute("DELETE FROM snippet_sources WHERE source=?", (path,))
                try:
                    k = add_snippets(db, extract_file(path), path, "file", mtime)
                    status = "ok"
                except Exception as e:
                    k, status = 0, f"error: {type(e).__name__}"
                    n_err += 1
                db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",
                           (path, mtime, st.st_size, k, status, now()))
                n_new += 1
                n_sql += k
                if n_new % 500 == 0:
                    db.commit()
                    print(f"    {seen} dosya görüldü, {n_new} işlendi, {n_sql} sorgu "
                          f"({(time.time() - t0) / 60:.1f} dk)")
    db.commit()
    print(f"  Dosya taraması bitti: {seen} dosya görüldü, {n_new} işlendi, {n_sql} sorgu, {n_err} okunamadı")


# =========================== 4. RAPOR ===========================
def reanalyze(db):
    """Kayıtlı tüm sorguların tablo referanslarını güncel kurallarla yeniden çıkarır (Oracle/dosya gerekmez)."""
    db.execute("DELETE FROM snippet_refs")
    db.execute("DELETE FROM snippet_joins")
    rows = db.execute("SELECT id, text FROM snippets").fetchall()
    for sid, text in rows:
        kind, target, refs, joins = analyze(text)
        db.execute("UPDATE snippets SET kind=?, target=? WHERE id=?", (kind, target, sid))
        db.executemany("INSERT INTO snippet_refs VALUES(?,?,?,?)", [(sid, q, n, r) for q, n, r in refs])
        db.executemany("INSERT INTO snippet_joins VALUES(?,?,?,?,?,?,?)", [(sid, *j) for j in joins])
    db.commit()


def resolve_refs(db):
    """Tablo adlarını kataloğa eşler. Şemasız adlar (FROM X7) için sırayla: public synonym, adın tek bir şemada
    olması, aynı dosyada/nesnede şemasıyla geçen şemalar ("ev şemaları"), dosya yolundaki sicil klasörü."""
    reanalyze(db)
    catalog = {}
    for o, n in db.execute("SELECT owner, name FROM objects"):
        catalog.setdefault(n, set()).add(o)
    syn = {(o, n): (to, tn) for o, n, to, tn in db.execute("SELECT owner, name, table_owner, table_name FROM synonyms")}
    personal = re.compile(PERSONAL_REGEX)
    owners = {o for os_ in catalog.values() for o in os_}

    sources = {}  # snippet -> kaynaklar
    for sid, src in db.execute("SELECT snippet_id, source FROM snippet_sources"):
        sources.setdefault(sid, []).append(src)
    refs = db.execute("SELECT snippet_id, qual, name, role FROM snippet_refs").fetchall()

    # Ev şemaları: kaynakta şemasıyla yazılmış ve katalogda bulunan referansların şemaları (+ yoldaki sicil)
    home = {}
    for sid, q, n, role in refs:
        if q and q in catalog.get(n, ()):
            for src in sources.get(sid, []):
                home.setdefault(src, {}).setdefault(q, 0)
                home[src][q] += 1
    for src in {s for lst in sources.values() for s in lst}:
        if src.startswith(("view:", "plsql:")):
            o = src.split(":", 1)[1].split(".")[0]
            home.setdefault(src, {}).setdefault(o, 0)
            home[src][o] += 1000
        for part in re.split(r"[\\/]", src.upper()):
            if personal.fullmatch(part) or part in owners:
                home.setdefault(src, {}).setdefault(part, 0)
                home[src][part] += 100
    # Aynı kaynakta yazılan (CTAS) şemasız tablolar: geçici ara tablo olabilir
    created = {}
    for sid, q, n, role in refs:
        if role == "write":
            for src in sources.get(sid, []):
                created.setdefault(src, set()).add(n)

    def resolve(sid, q, n, role):
        if q:
            if q in catalog.get(n, ()):
                return (q, n)
            return syn.get((q, n))
        if ("PUBLIC", n) in syn:
            return syn[("PUBLIC", n)]
        cands = catalog.get(n, set())
        # Bu dosyada CREATE TABLE ile oluşturulan şemasız ad (X, TEMP2 ...) script'in kendi ara tablosudur:
        # başka bir şemada aynı adlı tablo olsa bile ona bağlanmaz, sadece dosyanın kendi şemasına bağlanabilir.
        local = any(n in created.get(src, ()) for src in sources.get(sid, []))
        if len(cands) == 1 and not local:
            return (next(iter(cands)), n)
        score = {}
        for src in sources.get(sid, []):
            for o, w in home.get(src, {}).items():
                if not cands or o in cands:
                    score[o] = score.get(o, 0) + w
        if score and (cands or role == "write"):
            return (max(score, key=score.get), n)
        return None

    # Dönemsel tablo aileleri (ECL_RESULTS_202504, _202505 ... -> ECL_RESULTS_{YYYYMM})
    fam = {}
    for o, n in db.execute("SELECT owner, name FROM objects"):
        f = family_of(n)
        if f:
            fam.setdefault((o, f), []).append(n)
    db.execute("DELETE FROM families")
    db.executemany("INSERT INTO families VALUES(?,?,?,?,?)",
                   [(o, f, len(v), min(v), max(v)) for (o, f), v in fam.items() if len(v) >= 2])
    fam_owners = {}
    for (o, f), v in fam.items():
        if len(v) >= 2:
            fam_owners.setdefault(f, set()).add(o)

    db.execute("DELETE FROM snippet_tables")
    db.execute("DELETE FROM snippet_families")
    out, fam_out, temp, missing = [], [], 0, {}
    for sid, q, n, role in refs:
        hit = resolve(sid, q, n, role)
        f = family_of(n)
        if hit:
            out.append((sid, hit[0], hit[1], role))
            if f:
                fam_out.append((sid, hit[0], f, role))
        elif f and (q in fam_owners.get(f, ()) if q else len(fam_owners.get(f, ())) == 1):
            fam_out.append((sid, q or next(iter(fam_owners[f])), f, role))  # silinmiş dönem, aile hâlâ var
        elif any(n in created.get(src, ()) for src in sources.get(sid, [])):
            temp += 1
        else:
            key = f"{q}.{n}" if q else n
            missing[key] = missing.get(key, 0) + 1
    db.executemany("INSERT INTO snippet_tables VALUES(?,?,?,?)", out)
    db.executemany("INSERT INTO snippet_families VALUES(?,?,?,?)", fam_out)

    # Join kenarları: sorgulardaki a.x = b.y eşitlikleri, iki kolon da katalogda varsa.
    # Taraflardan biri script içindeki ara tabloysa (CREATE TABLE X7 AS SELECT ... FROM HIST.CASH_LOANS ...),
    # aynı isimli kolon, ara tablonun kaynak tablolarına kadar geri izlenir (en fazla 4 adım).
    cols = {}
    for o, t, c in db.execute("SELECT owner, table_name, column_name FROM columns"):
        cols.setdefault((o, t), set()).add(c)
    write_of = {}
    for sid, q, n, role in refs:
        if role == "write":
            write_of[sid] = (q, n)
    reads_of = {}
    for sid, q, n, role in refs:
        if role == "read" and sid in write_of:
            reads_of.setdefault(sid, []).append((q, n))
    ctas_src = {}  # (kaynak dosya, ara tablo adı) -> o tabloyu üreten sorgunun okuduğu tablolar
    for sid, (q, n) in write_of.items():
        for src in sources.get(sid, []):
            ctas_src.setdefault((src, n), set()).update(reads_of.get(sid, []))

    def trace(sid, src, q, n, col, depth=0):
        hit = resolve(sid, q, n, "read")
        if hit and col in cols.get(hit, ()):
            return {hit}
        if depth >= 4:
            return set()
        found = set()
        for q2, n2 in ctas_src.get((src, n), ()):
            if (q2, n2) != (q, n):
                found |= trace(sid, src, q2, n2, col, depth + 1)
        return found

    edges = {}
    for sid, q1, t1, c1, q2, t2, c2 in db.execute("SELECT * FROM snippet_joins").fetchall():
        for src in sources.get(sid, []) or [None]:
            direct = True
            h1, h2 = resolve(sid, q1, t1, "read"), resolve(sid, q2, t2, "read")
            if not (h1 and c1 in cols.get(h1, ())):
                c = trace(sid, src, q1, t1, c1)
                h1, direct = (next(iter(c)) if len(c) == 1 else None), False
            if not (h2 and c2 in cols.get(h2, ())):
                c = trace(sid, src, q2, t2, c2)
                h2, direct = (next(iter(c)) if len(c) == 1 else None), False
            if not (h1 and h2) or h1 == h2:
                continue
            key = (h1[0], h1[1], c1, h2[0], h2[1], c2)
            if (h1[1], c1) > (h2[1], c2):
                key = (h2[0], h2[1], c2, h1[0], h1[1], c1)
            e = edges.setdefault(key, [set(), set(), False])
            e[0].add(sid)
            if src and not src.startswith(("view:", "plsql:", "vsql:")):
                e[1].add(src)
            e[2] = e[2] or direct
    db.execute("DROP TABLE IF EXISTS join_edges")
    db.execute("""CREATE TABLE join_edges(owner1 TEXT, table1 TEXT, col1 TEXT, owner2 TEXT, table2 TEXT, col2 TEXT,
                  n_queries INTEGER, n_files INTEGER, evidence TEXT)""")
    db.executemany("INSERT INTO join_edges VALUES(?,?,?,?,?,?,?,?,?)",
                   [(*k, len(v[0]), len(v[1]), "dogrudan" if v[2] else "ara_tablo_uzerinden")
                    for k, v in edges.items()])
    db.commit()

    total = len(refs)
    print(f"  {total} tablo referansı: {len(out)} eşlendi (%{100 * len(out) // max(total, 1)}), "
          f"{temp} script içinde oluşturulup silinen ara tablo, "
          f"{len(fam_out) - sum(1 for r in out if family_of(r[2]))} silinmiş dönem (ailesi var), "
          f"{sum(missing.values())} bulunamadı")
    n_direct = sum(1 for v in edges.values() if v[2])
    print(f"  {len([1 for v in fam.values() if len(v) >= 2])} dönemsel tablo ailesi, {len(edges)} doğrulanmış join "
          f"(kolon çifti): {n_direct} doğrudan, {len(edges) - n_direct} ara tablo üzerinden geri izlenerek")
    print("  En sık bulunamayan adlar (silinmiş tablo, yetki yok, db link ...):")
    for k, v in sorted(missing.items(), key=lambda x: -x[1])[:15]:
        print(f"    {k:<45} {v}")


def build_fts(db):
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(text, content='snippets', "
                   "content_rowid='id')")
        db.execute("INSERT INTO snippets_fts(snippets_fts) VALUES('rebuild')")
        db.commit()
        print("  Tam metin arama indeksi kuruldu")
    except sqlite3.OperationalError as e:
        print(f"  FTS5 yok, tam metin indeksi atlandı ({e})")


def report(db):
    resolve_refs(db)
    build_fts(db)
    inv = os.path.join(KB_DIR, "tablo_envanteri.csv")
    rows = db.execute("""
        SELECT o.owner, o.name, o.type, o.personal, o.comments, o.num_rows, o.created, o.last_ddl, o.last_analyzed,
               o.mod_time, COALESCE(u.n_files, 0), COALESCE(u.n_queries, 0), COALESCE(u.n_db_objects, 0),
               COALESCE(u.vsql_executions, 0), u.last_used,
               (SELECT COUNT(*) FROM columns c WHERE c.owner = o.owner AND c.table_name = o.name)
        FROM objects o LEFT JOIN v_usage u ON u.owner = o.owner AND u.table_name = o.name
        ORDER BY COALESCE(u.n_files, 0) DESC, COALESCE(u.n_db_objects, 0) DESC, o.owner, o.name""").fetchall()
    with open(inv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sema", "tablo", "tip", "kisisel", "aciklama", "satir_sayisi", "olusturma", "son_ddl",
                    "son_istatistik", "son_dml", "kullanan_dosya", "gectigi_sorgu", "kullanan_db_nesnesi",
                    "vsql_calisma", "son_kullanim", "kolon_sayisi"])
        w.writerows(rows)
    lin = os.path.join(KB_DIR, "koken.csv")
    with open(lin, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["hedef_sema", "hedef_tablo", "kaynak_sema", "kaynak_tablo", "tur"])
        w.writerows(db.execute("SELECT * FROM v_lineage ORDER BY 1, 2, 3, 4"))

    jn = os.path.join(KB_DIR, "joinler.csv")
    with open(jn, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sema1", "tablo1", "kolon1", "sema2", "tablo2", "kolon2", "sorgu_sayisi", "dosya_sayisi", "kanit"])
        w.writerows(db.execute("SELECT * FROM join_edges ORDER BY n_files DESC, n_queries DESC"))

    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    print("\n================ ÖZET ================")
    print(f"Şema: {q('SELECT COUNT(DISTINCT owner) FROM objects')} | Tablo/view: {q('SELECT COUNT(*) FROM objects')}"
          f" | Kolon: {q('SELECT COUNT(*) FROM columns')}")
    print(f"Açıklamalı tablo: {q('SELECT COUNT(*) FROM objects WHERE comments IS NOT NULL')} | "
          f"Açıklamalı kolon: {q('SELECT COUNT(*) FROM columns WHERE comments IS NOT NULL')}")
    print(f"Taranan dosya: {q('SELECT COUNT(*) FROM files')} | SQL içeren dosya: "
          f"{q('SELECT COUNT(*) FROM files WHERE n_snippets > 0')} | Benzersiz sorgu: {q('SELECT COUNT(*) FROM snippets')}")
    print(f"En az bir sorguda geçen tablo: {q('SELECT COUNT(*) FROM v_usage')} | "
          f"Köken ilişkisi: {q('SELECT COUNT(*) FROM v_lineage')}")
    print("\nEn çok kullanılan 30 tablo (dosya sayısına göre):")
    for r in db.execute("""SELECT owner || '.' || table_name, n_files, n_queries, last_used FROM v_usage
                           ORDER BY n_files DESC, n_queries DESC LIMIT 30"""):
        print(f"  {r[0]:<50} {r[1]:>5} dosya {r[2]:>6} sorgu  son: {r[3] or '-'}")
    print("\nEn çok kullanılan 15 join:")
    for r in db.execute("""SELECT owner1||'.'||table1||'.'||col1, owner2||'.'||table2||'.'||col2, n_files
                           FROM join_edges ORDER BY n_files DESC, n_queries DESC LIMIT 15"""):
        print(f"  {r[0]:<55} = {r[1]:<55} {r[2]:>4} dosya")
    print("\nEn çok kullanılan 10 dönemsel tablo ailesi:")
    for r in db.execute("""SELECT sf.owner||'.'||sf.family, COUNT(DISTINCT ss.source), f.n_tables, f.last_table
                           FROM snippet_families sf JOIN snippet_sources ss ON ss.snippet_id = sf.snippet_id
                           LEFT JOIN families f ON f.owner = sf.owner AND f.family = sf.family
                           WHERE sf.role = 'read' GROUP BY sf.owner, sf.family ORDER BY 2 DESC LIMIT 10"""):
        print(f"  {r[0]:<50} {r[1]:>4} dosya  {r[2] or 0:>3} dönem, en yeni: {r[3] or '-'}")
    print(f"\nYazıldı: {inv}\n         {lin}\n         {jn}\n         {os.path.join(KB_DIR, 'kb.sqlite')}")


def main():
    db = open_kb()
    if done(db, "*", "oracle_full"):
        last = db.execute("SELECT done_at FROM harvest_log WHERE owner='*' AND step='oracle_full'").fetchone()[0]
        print(f"1-2/4 Oracle bilgileri güncel (son toplama: {last}), Oracle'a bağlanılmıyor.")
    else:
        conn = connect()
        owners = list_owners(conn)
        print(f"{len(owners)} şema bulundu")
        print("\n1/4 Metadata toplanıyor (ilk çalışmada uzun sürer, kaldığı yerden devam eder)...")
        errors = harvest_metadata(conn, db, owners)
        print("\n2/4 View, PL/SQL ve V$SQL okunuyor...")
        errors += harvest_db_code(conn, db, owners)
        conn.close()
        if errors == 0:
            mark(db, "*", "oracle_full")
        else:
            print(f"  {errors} şema hata verdi; bir sonraki çalıştırmada sadece onlar tekrar denenecek.")
    print("\n3/4 Klasörler taranıyor (sadece yeni / değişmiş dosyalar)...")
    crawl_files(db)
    print("\n4/4 Rapor hazırlanıyor...")
    report(db)
    db.close()


if __name__ == "__main__":
    main()
