# -*- coding: utf-8 -*-
"""
kb_profile.py - Kullanılan tabloların kolon profillerini Oracle istatistiklerinden çıkarır (veri okumaz)

Tabloları taramaz; sadece Oracle'ın zaten tuttuğu optimizer istatistiklerini okur:
  - Sayısal ve tarih kolonları için en küçük / en büyük değer (ör. 20190101 .. 20261031 -> YYYYMMDD tarih)
  - Az sayıda farklı değeri olan kod kolonları için değerler ve oranları (frekans histogramlarından),
    ör. STATUS: A %62, K %30, T %8
Ad, adres, telefon, kimlik no gibi kişisel olabilecek kolonlar ve metin kolonlarının aralıkları atlanır.

Sonuç kb.sqlite içindeki col_profile tablosuna yazılır; kb_annotate tablo kartlarında kullanır.
Kurulum: kb_builder.py ile aynı klasörde olmalı. 7 gün içinde profillenen tabloları tekrar okumaz.
"""
import os
import re
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_builder as kb  # noqa: E402

MAX_DISTINCT_FOR_VALUES = 50     # bundan fazla farklı değeri olan kolonun değerleri alınmaz
TOP_VALUES = 15
SKIP_COLUMN_RE = re.compile(r"(NAME|_AD$|^AD$|^AD_|ADI$|SOYAD|UNVAN|TITLE|ADDR|ADRES|TEL|PHONE|GSM|MAIL|TCKN|"
                            r"KIMLIK|VKN|IBAN|ACCOUNT_NO|ACCT_NO|CARD_NO|KART_NO|BIRTH|DOGUM|PASS|SIFRE|NOTE|ACIKLAMA|"
                            r"DESC|COMMENT)", re.I)

PROFILE_SQL = """CREATE TABLE IF NOT EXISTS col_profile(owner TEXT, table_name TEXT, column_name TEXT, low TEXT,
    high TEXT, top_values TEXT, PRIMARY KEY(owner, table_name, column_name));
CREATE TABLE IF NOT EXISTS lookup_values(owner TEXT, table_name TEXT, column_name TEXT, lookup_table TEXT,
    code TEXT, descr TEXT);"""
LOOKUP_MAX_ROWS = 2000           # bundan büyük referans tablosu okunmaz
DESC_COL_RE = re.compile(r"(DESC|DSC|EXPL|ACIKLAMA|TANIM|NAME|_NM$|_AD$|ADI$|TXT|TEXT|DEF|LABEL)", re.I)


def dec_number(b):
    """Oracle NUMBER iç biçimini (RAW) sayıya çevirir."""
    if not b:
        return None
    if b == b"\x80":
        return "0"
    if b[0] & 0x80:
        exp, digits, sign = (b[0] & 0x7F) - 65, [x - 1 for x in b[1:]], 1
    else:
        d = b[1:-1] if b[-1] == 102 else b[1:]
        exp, digits, sign = ((~b[0]) & 0x7F) - 65, [101 - x for x in d], -1
    val = sum(Decimal(dg) * Decimal(100) ** (exp - i) for i, dg in enumerate(digits))
    val = sign * val
    return format(val.normalize(), "f") if val != val.to_integral() else str(int(val))


def dec_date(b):
    if not b or len(b) < 7:
        return None
    return f"{(b[0] - 100) * 100 + (b[1] - 100):04d}-{b[2]:02d}-{b[3]:02d}"


def decode(raw, data_type):
    try:
        dt_ = (data_type or "").upper()
        if dt_.startswith(("NUMBER", "INTEGER", "FLOAT")):
            return dec_number(raw)
        if dt_.startswith(("DATE", "TIMESTAMP")):
            return dec_date(raw)
    except Exception:
        return None
    return None  # metin kolonlarının uç değerleri kişisel veri olabilir: alınmaz


def profile_table(conn, db, owner, table):
    types = dict(db.execute("SELECT column_name, data_type FROM columns WHERE owner=? AND table_name=?",
                            (owner, table)).fetchall())
    stats = kb.fetch(conn, """SELECT column_name, num_distinct, low_value, high_value, histogram
                              FROM all_tab_col_statistics WHERE owner = :o AND table_name = :t""",
                     {"o": owner, "t": table})
    rows, want_hist = {}, []
    for col, nd, low, high, hist in stats:
        if SKIP_COLUMN_RE.search(col):
            continue
        rows[col] = [decode(low, types.get(col)), decode(high, types.get(col)), None]
        if hist in ("FREQUENCY", "TOP-FREQUENCY") and nd is not None and nd <= MAX_DISTINCT_FOR_VALUES:
            want_hist.append(col)
    for col in want_hist:
        hist = kb.fetch(conn, """SELECT endpoint_number, endpoint_value, endpoint_actual_value FROM all_tab_histograms
                                 WHERE owner = :o AND table_name = :t AND column_name = :c
                                 ORDER BY endpoint_number""", {"o": owner, "t": table, "c": col})
        if not hist:
            continue
        total, prev, vals = hist[-1][0] or 0, 0, []
        is_num = (types.get(col) or "").upper().startswith(("NUMBER", "INTEGER"))
        for num, val, actual in hist:
            cnt, prev = (num or 0) - prev, num or 0
            v = actual if actual is not None else (format(Decimal(str(val)).normalize(), "f") if is_num and val is not None
                                                    else None)
            if v is not None:
                vals.append((str(v).strip(), cnt))
        vals.sort(key=lambda x: -x[1])
        if total and vals:
            rows[col][2] = ", ".join(f"{v} %{100 * c / total:.0f}" for v, c in vals[:TOP_VALUES]) + \
                           (" …" if len(vals) > TOP_VALUES else "")
    db.execute("DELETE FROM col_profile WHERE owner=? AND table_name=?", (owner, table))
    db.executemany("INSERT INTO col_profile VALUES(?,?,?,?,?,?)",
                   [(owner, table, c, lo, hi, tv) for c, (lo, hi, tv) in rows.items() if lo or hi or tv])
    return len(want_hist)


def lookup_pairs(db):
    """Kullanılan tabloların kod kolonu -> LOOKUP tablosunun kod kolonu eşleşmeleri (doğrulanmış join'lerden)."""
    pairs = []
    for o1, t1, c1, o2, t2, c2 in db.execute(
            "SELECT owner1, table1, col1, owner2, table2, col2 FROM join_edges").fetchall():
        for (ao, at, ac), (bo, bt, bc) in (((o1, t1, c1), (o2, t2, c2)), ((o2, t2, c2), (o1, t1, c1))):
            if (bo.endswith("LOOKUP") or bt.startswith("LU_")) and not (ao.endswith("LOOKUP") or at.startswith("LU_")):
                pairs.append((ao, at, ac, bo, bt, bc))
    return pairs


def read_lookups(conn, db):
    """Kod kolonlarının anlamlarını küçük LOOKUP tablolarından okur (referans verisi, müşteri verisi değil)."""
    pairs = lookup_pairs(db)
    cache, n = {}, 0
    db.execute("DELETE FROM lookup_values")
    for ao, at, ac, bo, bt, bc in pairs:
        key = (bo, bt, bc)
        if key not in cache:
            cache[key] = {}
            nrows = (db.execute("SELECT num_rows FROM objects WHERE owner=? AND name=?", (bo, bt)).fetchone()
                     or (None,))[0]
            if nrows is not None and nrows > LOOKUP_MAX_ROWS:
                continue
            cols = [r[0] for r in db.execute("""SELECT column_name FROM columns WHERE owner=? AND table_name=?
                                                AND data_type LIKE '%CHAR%' ORDER BY column_id""", (bo, bt))]
            desc = next((c for c in cols if c != bc and DESC_COL_RE.search(c)), None) or \
                next((c for c in cols if c != bc), None)
            if not desc:
                continue
            try:
                rows = kb.fetch(conn, f'SELECT "{bc}", "{desc}" FROM "{bo}"."{bt}" '
                                      f'FETCH FIRST {LOOKUP_MAX_ROWS + 1} ROWS ONLY')
            except Exception as e:
                print(f"  LOOKUP {bo}.{bt}: {str(e)[:100]}")
                continue
            if len(rows) <= LOOKUP_MAX_ROWS:
                cache[key] = {str(c).strip(): str(d).strip()[:80] for c, d in rows if c is not None and d is not None}
        vals = cache[key]
        if vals:
            db.executemany("INSERT INTO lookup_values VALUES(?,?,?,?,?,?)",
                           [(ao, at, ac, f"{bo}.{bt}", c, d) for c, d in vals.items()])
            n += 1
    db.commit()
    print(f"LOOKUP: {len(pairs)} kod kolonu-referans tablo eşleşmesi, {n} tanesinin kod anlamları okundu "
          f"({len([k for k, v in cache.items() if v])} referans tablosu)")


def main():
    db = kb.open_kb()
    db.executescript(PROFILE_SQL)
    tables = db.execute("""SELECT u.owner, u.table_name FROM v_usage u
                           JOIN objects o ON o.owner = u.owner AND o.name = u.table_name AND o.type = 'TABLE'
                           ORDER BY u.n_files DESC""").fetchall()
    todo = [(o, t) for o, t in tables if not kb.done(db, f"{o}.{t}", "profile")]
    print(f"{len(tables)} kullanılan tablo, {len(todo)} tanesi profillenecek (sadece istatistik okunur)")
    need_lookup = not kb.done(db, "*", "lookup")
    if not todo and not need_lookup:
        return
    conn = kb.connect()
    conn.call_timeout = 120_000
    t0, n_err = time.time(), 0
    for i, (o, t) in enumerate(todo, 1):
        try:
            profile_table(conn, db, o, t)
            kb.mark(db, f"{o}.{t}", "profile")
        except Exception as e:
            n_err += 1
            print(f"  {o}.{t}: {str(e)[:120]}")
        if i % 50 == 0 or i == len(todo):
            db.commit()
            print(f"  {i}/{len(todo)} ({(time.time() - t0) / 60:.1f} dk, {n_err} hata)")
    if need_lookup:
        read_lookups(conn, db)
        kb.mark(db, "*", "lookup")
    conn.close()
    n = db.execute("SELECT COUNT(*), SUM(top_values IS NOT NULL), SUM(low IS NOT NULL) FROM col_profile").fetchone()
    print(f"Bitti: {n[0]} kolon profili ({n[1]} kolonda kod değerleri, {n[2]} kolonda aralık)")
    db.close()


if __name__ == "__main__":
    main()
