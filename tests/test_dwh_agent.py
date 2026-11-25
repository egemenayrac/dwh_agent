# -*- coding: utf-8 -*-
"""dwh_agent çekirdek birim ve regresyon testleri (Oracle ve LLM olmadan yerel çalışır)."""
import io
import json
import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ajan_core as core  # noqa: E402
import kb_annotate  # noqa: E402
import kb_builder as kb  # noqa: E402
import kb_profile  # noqa: E402


def _make_test_db():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(kb.SCHEMA_SQL)
    db.executescript(kb_profile.PROFILE_SQL)
    db.executescript(kb_annotate.CARD_SQL)
    db.execute("CREATE VIEW IF NOT EXISTS v_usage AS SELECT owner, table_name, COUNT(*) AS n_files FROM snippet_tables GROUP BY owner, table_name")
    core.init_db(db)
    return db


def test_kb_profile_skips_sensitive_columns_before_low_high_decode(monkeypatch):
    db = _make_test_db()
    db.executemany(
        "INSERT INTO columns(owner, table_name, column_id, column_name, data_type) VALUES(?,?,?,?,?)",
        [
            ("RISK", "CUSTOMERS", 1, "TCKN", "NUMBER"),
            ("RISK", "CUSTOMERS", 2, "VKN", "NUMBER"),
            ("RISK", "CUSTOMERS", 3, "DOGUM_TARIHI", "DATE"),
            ("RISK", "CUSTOMERS", 4, "DPD_DAYS", "NUMBER"),
        ],
    )
    # Oracle NUMBER RAW encoding for 100 -> b"\xc2\x02", 0 -> b"\x80"
    fake_stats = [
        ("TCKN", 1000, b"\xc2\x02", b"\xc2\x02", "NONE"),
        ("VKN", 1000, b"\xc2\x02", b"\xc2\x02", "NONE"),
        ("DOGUM_TARIHI", 1000, bytes([119, 190, 1, 1, 1, 1, 1]), bytes([120, 100, 12, 31, 1, 1, 1]), "NONE"),
        ("DPD_DAYS", 10, b"\x80", b"\xc2\x02", "NONE"),
    ]
    monkeypatch.setattr(kb, "fetch", lambda conn, sql, params=None: fake_stats)

    kb_profile.profile_table(None, db, "RISK", "CUSTOMERS")
    cols = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT column_name, low, high FROM col_profile WHERE owner='RISK' AND table_name='CUSTOMERS'")}
    assert "TCKN" not in cols
    assert "VKN" not in cols
    assert "DOGUM_TARIHI" not in cols
    assert cols["DPD_DAYS"] == ("0", "100")


def test_kb_annotate_reads_api_key_from_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_annotate, "LLM_API_KEY", "")
    key_file = tmp_path / "ibthink_anahtar.txt"
    key_file.write_text("Bearer dosya-anahtari\n", encoding="utf-8")
    monkeypatch.setattr(kb_annotate, "KEY_FILE", str(key_file))
    monkeypatch.delenv("IBTHINK_API_KEY", raising=False)

    assert kb_annotate.get_api_key() == "dosya-anahtari"

    monkeypatch.setenv("IBTHINK_API_KEY", "Bearer ortam-anahtari")
    assert kb_annotate.get_api_key() == "ortam-anahtari"


def test_bilgi_ara_turkish_case_insensitive_and_use_verified_flag():
    db = _make_test_db()
    db.execute(
        "INSERT INTO objects(owner, name, type, comments, personal) VALUES(?,?,?,?,?)",
        ("DWH", "SUBE_OZET", "TABLE", "Şube bazlı Yakın İzleme ve müşteri gecikme özeti", 0),
    )
    db.execute(
        "INSERT INTO table_cards(owner, table_name, input_hash, card, status, created_at) VALUES(?,?,?,?,?,?)",
        ("DWH", "SUBE_OZET", "h1", json.dumps({"ozet": "Şube ve Müşteri İzleme tablosu", "satir_seviyesi": "Şube"}, ensure_ascii=False), "ok", "2026-11-19"),
    )
    db.execute(
        "INSERT INTO families(owner, family, n_tables, first_table, last_table) VALUES(?,?,?,?,?)",
        ("DWH", "IZLEME_{DONEM}", 3, "IZLEME_202601", "IZLEME_202603"),
    )
    core.save_verified(db, "Şube bazlı izleme adedi nedir?", "SELECT COUNT(*) FROM DWH.SUBE_OZET", "izleme")

    ctx_normal = core.Ctx(db, conn=None, use_verified=True)
    out = ctx_normal.bilgi_ara(["şube", "İzleme", "MÜŞTERİ"])
    assert "DWH.SUBE_OZET" in out
    assert "DWH.IZLEME_{DONEM}" in out
    assert "EKİBİN ONAYLADIĞI SORGULAR" in out

    ctx_eval = core.Ctx(db, conn=None, use_verified=False)
    out_eval = ctx_eval.bilgi_ara(["şube", "İzleme"])
    assert "DWH.SUBE_OZET" in out_eval
    assert "EKİBİN ONAYLADIĞI SORGULAR" not in out_eval


def test_feedback_and_verified_excel_export():
    db = _make_test_db()
    core.save_verified(db, "NPL sayısı nedir?", "SELECT COUNT(*) FROM DWH.LOANS", "gecikme")
    core.save_feedback(db, "Yanlış soru", "SELECT 1 FROM DUAL", sonuc="yanlis", notlar="Yanlış tablo seçildi")

    neg = core.list_feedback(db, sonuc="yanlis")
    assert len(neg) == 1
    assert neg.iloc[0]["notlar"] == "Yanlış tablo seçildi"

    xls_bytes, count = core.export_verified_excel(db)
    assert count == 1
    exported = pd.read_excel(io.BytesIO(xls_bytes))
    assert list(exported.columns) == ["soru", "dogru_sql", "kategori"]
    assert exported.iloc[0]["dogru_sql"] == "SELECT COUNT(*) FROM DWH.LOANS"


def test_evaluate_row_limit_parity_above_show_rows(monkeypatch):
    db = _make_test_db()
    total_rows = core.SHOW_ROWS + 250  # 5250 > SHOW_ROWS(5000)

    def fake_fetch_df(conn, sql, max_rows):
        n = min(total_rows, max_rows)
        return pd.DataFrame({"ID": list(range(n))}), total_rows > max_rows

    def fake_run(sess, ctx, on_step=None, auto_approve=False):
        sql = "SELECT ID FROM DWH.BIG_TABLE"
        df, more = fake_fetch_df(ctx.conn, sql, core.SHOW_ROWS)
        ctx.last_df, ctx.last_sql, ctx.last_more = df, sql, more
        sess.result = {"cevap": "Sonuç hazır.", "sql": sql, "varsayimlar": ""}
        sess.status = "done"
        return "done"

    monkeypatch.setattr(core, "fetch_df", fake_fetch_df)
    monkeypatch.setattr(core, "run", fake_run)

    _, res = core.evaluate(db, conn=object(), rows=[("Büyük tablo", "SELECT ID FROM DWH.BIG_TABLE", "test")])
    assert res.iloc[0]["sonuc"] == "DOĞRU"
    assert res.iloc[0]["f1"] == 1.0


def test_kb_builder_sqlglot_and_regex_fallback():
    sql = """
    WITH aktif AS (
        SELECT c.cust_id, l.loan_id
        FROM risk.customers c
        JOIN risk.loans l ON c.cust_id = l.cust_id
    )
    SELECT a.cust_id, b.col_amt
    FROM aktif a
    LEFT JOIN dwh.collaterals b ON a.loan_id = b.loan_id
    """
    kind, target, refs, joins = kb.analyze(sql)
    assert kind == "select"
    assert target is None
    assert ("RISK", "CUSTOMERS", "read") in refs
    assert ("RISK", "LOANS", "read") in refs
    assert ("DWH", "COLLATERALS", "read") in refs
    assert all(r[1] != "AKTIF" for r in refs)
    assert ("RISK", "CUSTOMERS", "CUST_ID", "RISK", "LOANS", "CUST_ID") in joins

    # CTAS test
    ctas = "CREATE TABLE a12345.tmp_pd AS SELECT t.id FROM risk.base_tbl t"
    kind2, target2, refs2, _ = kb.analyze(ctas)
    assert kind2 == "ctas"
    assert target2 == "A12345.TMP_PD"
    assert ("A12345", "TMP_PD", "write") in refs2
    assert ("RISK", "BASE_TBL", "read") in refs2

    # Fallback on non-standard / broken SQL syntax
    broken = "SELECT * FROM risk.tbl_a a, risk.tbl_b b WHERE a.id = b.id AND @@@BROKEN@@@"
    kind3, _, refs3, joins3 = kb.analyze(broken)
    assert kind3 == "select"
    assert ("RISK", "TBL_A", "read") in refs3
    assert ("RISK", "TBL_B", "read") in refs3
    assert ("RISK", "TBL_A", "ID", "RISK", "TBL_B", "ID") in joins3


def test_check_select_guards():
    assert core.check_select("SELECT 1 FROM DUAL;") == "SELECT 1 FROM DUAL"
    for bad in [
        "DELETE FROM risk.loans",
        "SELECT 1 FROM DUAL; DROP TABLE risk.loans",
        "CREATE TABLE x AS SELECT 1 FROM DUAL",
    ]:
        try:
            core.check_select(bad)
            assert False, f"Engellenmeliydi: {bad}"
        except ValueError:
            pass


def test_tablo_olustur_reddeder_baska_sema_gecersiz_ad_ve_yazma_sorgusu():
    class _Conn:
        username = "a12345"
    ctx = core.Ctx(_make_test_db(), _Conn())
    sel = "SELECT 1 FROM DUAL"
    prep, err = ctx.tablo_hazirla("HIST.YENI", sel)
    assert prep is None and "kendi şemasında" in err
    prep, err = ctx.tablo_hazirla("X; DROP TABLE Y", sel)
    assert prep is None and "Geçersiz tablo adı" in err
    prep, err = ctx.tablo_hazirla("YENI", "DELETE FROM HIST.CASH_LOANS")
    assert prep is None and err.startswith("ÇALIŞTIRILMADI")


def test_tablo_olustur_olcum_modunda_calismaz():
    sess, ctx = core.Session("x"), core.Ctx(_make_test_db(), None)
    calls = [{"id": "c1", "function": {"name": "tablo_olustur",
                                       "arguments": json.dumps({"tablo_adi": "T", "sql": "SELECT 1 FROM DUAL"})}}]
    assert core._process_calls(sess, ctx, calls, None, auto_approve=True) == "continue"
    assert sess.pending is None
