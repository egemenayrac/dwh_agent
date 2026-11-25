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


def test_schemas_generate_openai_tools_and_validate_args():
    import schemas

    tools = schemas.build_openai_tools()
    names = [t["function"]["name"] for t in tools]
    expected = [
        "bilgi_ara", "tablo_karti", "join_yolu", "koken", "son_donem",
        "sql_calistir", "tekillik_kontrol", "plan_onayla", "kullaniciya_sor",
        "tablo_olustur", "cevap_ver",
    ]
    assert names == expected
    assert core.TOOLS == tools

    for t in tools:
        assert t["type"] == "function"
        params = t["function"]["parameters"]
        assert params["type"] == "object"
        assert "title" not in params
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)

    # Doğrulama ve tip dönüşümü
    ok_args, err = schemas.validate_tool_args("bilgi_ara", '{"kelimeler": "gecikme, dpd"}')
    assert err is None and ok_args == {"kelimeler": ["gecikme", "dpd"]}

    bad_args, bad_err = schemas.validate_tool_args("sql_calistir", '{"amac": "test"}')
    assert bad_args is None and bad_err is not None and "sql" in bad_err

    # Kart şemaları (kb_annotate.py)
    tcard = schemas.TableCardSchema.model_validate({
        "ozet": "Kredi bakiyeleri",
        "anahtar_kolonlar": "LOAN_ID",
        "tarih_kolonlari": ["AS_OF_DT"],
        "tuzaklar": ["AS_OF_DT filtresi şart"],
    })
    assert tcard.anahtar_kolonlar == ["LOAN_ID"]
    assert tcard.tarih_kolonlari[0].kolon == "AS_OF_DT"


def test_openai_httpx_tenacity_retry_in_ajan_core_and_kb_annotate(monkeypatch):
    import httpx
    import openai
    import schemas

    # URL dönüşümü ve SSL doğrulama yardımcıları
    assert core.openai_base_url("https://ibthink.banka.local/v1/chat/completions") == "https://ibthink.banka.local/v1"
    assert core.build_httpx_verify(False) is False
    assert core.build_httpx_verify(True) is True

    # 1) ajan_core.llm_chat: 429 (Retry-After: 0) + 503 -> 200 OK (tool_call dahil)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        assert str(request.url) == "https://ibthink.local/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "rate limit"}})
        if attempts["n"] == 2:
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(200, json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "qwen3-27b",
            "choices": [{
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "<think>düşünüyor</think>Arıyorum.",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bilgi_ara", "arguments": '{"kelimeler": ["dpd"]}'},
                    }],
                },
            }],
        })

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    mock_client = openai.OpenAI(base_url="https://ibthink.local/v1", api_key="test-key", http_client=mock_http, max_retries=0)
    monkeypatch.setattr(core, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(core, "LLM_MODEL", "qwen3-27b")
    monkeypatch.setattr(core, "LLM_RETRIES", 4)
    monkeypatch.setattr(core, "_openai_client", lambda key: mock_client)
    monkeypatch.setattr(core._WaitWithRetryAfter, "__call__", lambda self, rs: 0.0)

    msg = core.llm_chat([{"role": "user", "content": "DPD nedir?"}], tools=core.TOOLS)
    assert attempts["n"] == 3
    assert msg["_finish"] == "tool_calls"
    assert msg["tool_calls"][0]["function"]["name"] == "bilgi_ara"

    # 2) 401 yetki hatasında tekrar denemeden hemen hata fırlatmalı
    auth_attempts = {"n": 0}

    def auth_fail(request: httpx.Request) -> httpx.Response:
        auth_attempts["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    auth_client = openai.OpenAI(
        base_url="https://ibthink.local/v1",
        api_key="bad",
        http_client=httpx.Client(transport=httpx.MockTransport(auth_fail)),
        max_retries=0,
    )
    monkeypatch.setattr(core, "_openai_client", lambda key: auth_client)
    try:
        core.llm_chat([{"role": "user", "content": "test"}])
        assert False, "401 hatası fırlatılmalıydı"
    except RuntimeError as exc:
        assert "401" in str(exc) and auth_attempts["n"] == 1

    # 3) kb_annotate.ask_llm: 502 geçici hatadan sonra Pydantic TableCardSchema ile doğrulanmış kart
    ann_attempts = {"n": 0}

    def ann_handler(request: httpx.Request) -> httpx.Response:
        ann_attempts["n"] += 1
        if ann_attempts["n"] == 1:
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})
        return httpx.Response(200, json={
            "id": "cmpl-2",
            "object": "chat.completion",
            "created": 1,
            "model": "qwen3-27b",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '{"ozet": "Nakit kredi tablosu", "is_alani": "IFRS 9", "satir_seviyesi": "kredi x gün", "anahtar_kolonlar": ["LOAN_ID"]}',
                },
            }],
        })

    ann_client = openai.OpenAI(
        base_url="https://ibthink.local/v1",
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(ann_handler)),
        max_retries=0,
    )
    monkeypatch.setattr(kb_annotate, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(kb_annotate, "LLM_RETRIES", 3)
    monkeypatch.setattr(kb_annotate, "_openai_client", lambda key: ann_client)
    monkeypatch.setattr(kb_annotate._WaitWithRetryAfter, "__call__", lambda self, rs: 0.0)

    card = kb_annotate.ask_llm("sys", "user", response_model=schemas.TableCardSchema)
    assert ann_attempts["n"] == 2
    assert card["ozet"] == "Nakit kredi tablosu"
    assert card["anahtar_kolonlar"] == ["LOAN_ID"]


def test_langgraph_hitl_interrupt_resume_and_mermaid(monkeypatch):
    import agent_graph

    db = _make_test_db()
    db.execute(
        "INSERT INTO objects(owner, name, type, comments, personal) VALUES(?,?,?,?,?)",
        ("DWH", "CASH_LOANS", "TABLE", "Nakit krediler", 0),
    )
    ctx = core.Ctx(db, conn=None)

    # Sırayla: bilgi_ara -> plan_onayla (interrupt!) -> cevap_ver (resume sonrası)
    scripted = iter([
        {
            "role": "assistant",
            "content": "",
            "_finish": "tool_calls",
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "bilgi_ara", "arguments": json.dumps({"kelimeler": ["CASH_LOANS"]})},
            }],
        },
        {
            "role": "assistant",
            "content": "",
            "_finish": "tool_calls",
            "tool_calls": [{
                "id": "tc2",
                "type": "function",
                "function": {"name": "plan_onayla", "arguments": json.dumps({"plan": "1. DWH.CASH_LOANS sayılacak."})},
            }],
        },
        {
            "role": "assistant",
            "content": "",
            "_finish": "tool_calls",
            "tool_calls": [{
                "id": "tc3",
                "type": "function",
                "function": {"name": "cevap_ver", "arguments": json.dumps({
                    "cevap": "DWH.CASH_LOANS tablosu üzerinde plan onaylandı ve işlem başarıyla tamamlandı.",
                    "sql": "SELECT COUNT(*) FROM DWH.CASH_LOANS",
                    "varsayimlar": "Son dönem baz alındı.",
                })},
            }],
        },
    ])
    monkeypatch.setattr(core, "llm_chat", lambda messages, tools=None, no_think=False: next(scripted))

    seen_steps = []
    sess = agent_graph.GraphSession("Nakit kredi sayısı nedir?")

    # Mermaid diyagramı kontrolü
    mermaid = sess.mermaid()
    assert "llm" in mermaid and "human_approval" in mermaid and "tools" in mermaid

    # İlk çalıştırma: bilgi_ara çalışır, plan_onayla'da interrupt() ile durur
    state1 = agent_graph.run_graph(sess, ctx, on_step=seen_steps.append)
    assert state1 == "waiting"
    assert sess.status == "waiting"
    assert sess.pending is not None
    assert sess.pending["kind"] == "plan_onayla"
    assert "CASH_LOANS" in sess.pending["text"]
    assert any(s["arac"] == "bilgi_ara" for s in sess.steps)

    # Kullanıcı onayıyla Command(resume=...) üzerinden devam
    state2 = agent_graph.resume_graph(sess, ctx, "Onaylandı, devam et.", on_step=seen_steps.append)
    assert state2 == "done"
    assert sess.status == "done"
    assert sess.pending is None
    assert sess.result is not None
    assert "CASH_LOANS" in sess.result["sql"]
    assert any(s["arac"] == "plan_onayla" and "Onaylandı" in s["cikti"] for s in sess.steps)


def test_mcp_server_dwh_tools_and_ews_bridge(tmp_path):
    import asyncio
    import mcp_server

    db = _make_test_db()
    db.execute(
        "INSERT INTO objects(owner, name, type, comments, num_rows, personal) VALUES(?,?,?,?,?,?)",
        ("DWH", "LOANS", "TABLE", "Kredi bakiyeleri", 1000, 0),
    )
    db.execute(
        "INSERT INTO objects(owner, name, type, comments, num_rows, personal) VALUES(?,?,?,?,?,?)",
        ("DWH", "CUSTOMERS", "TABLE", "Müşteri ana tablosu", 500, 0),
    )
    db.execute(
        "INSERT INTO columns(owner, table_name, column_id, column_name, data_type, num_distinct) VALUES(?,?,?,?,?,?)",
        ("DWH", "LOANS", 1, "CUST_ID", "NUMBER", 500),
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS join_edges(owner1 TEXT, table1 TEXT, col1 TEXT, "
        "owner2 TEXT, table2 TEXT, col2 TEXT, n_files INTEGER, evidence TEXT)"
    )
    db.execute(
        "INSERT INTO join_edges(owner1, table1, col1, owner2, table2, col2, n_files, evidence) VALUES(?,?,?,?,?,?,?,?)",
        ("DWH", "LOANS", "CUST_ID", "DWH", "CUSTOMERS", "CUST_ID", 12, "direct"),
    )
    db.execute(
        "INSERT INTO families(owner, family, n_tables, first_table, last_table) VALUES(?,?,?,?,?)",
        ("DWH", "PD_MAPPING_{DONEM}", 4, "PD_MAPPING_202601", "PD_MAPPING_202604"),
    )
    db.execute(
        "INSERT INTO snippets(id, hash, text, kind, target) VALUES(1, 'h1', 'CREATE TABLE DWH.LOANS AS SELECT * FROM DWH.CUSTOMERS', 'ctas', 'DWH.LOANS')"
    )
    db.execute("INSERT INTO snippet_tables(snippet_id, owner, table_name, role) VALUES(1, 'DWH', 'CUSTOMERS', 'read')")
    db.execute("INSERT INTO snippet_tables(snippet_id, owner, table_name, role) VALUES(1, 'DWH', 'LOANS', 'write')")

    # Örnek EWS llm_dogrulama.xlsx çıktısı (köprü testi için)
    ews_xlsx = tmp_path / "llm_dogrulama.xlsx"
    with pd.ExcelWriter(ews_xlsx, engine="openpyxl") as w:
        pd.DataFrame([
            {
                "Şirket": 101, "Şirket_Adi": "Örnek İnşaat A.Ş.", "Olay": "O1",
                "Olay_Tipi": "konkordato_iflas", "Seviye": "CRITICAL", "Durum": "açık",
                "Sinyale_Etkisi": "Kırmızı Alarm (Stage 3 Sinyali)", "Son_Haber": "2026-11-20",
                "Risk_Turu": "kredi", "Tanim": "Geçici mühlet kararı verildi",
            },
            {
                "Şirket": 102, "Şirket_Adi": "Beta Enerji A.Ş.", "Olay": "O1",
                "Olay_Tipi": "dava_ceza", "Seviye": "HIGH", "Durum": "kapandı 2026-10-01",
                "Sinyale_Etkisi": "Düşük Risk (Stage 1)", "Son_Haber": "2026-10-01",
                "Risk_Turu": "hukuki_itibar", "Tanim": "Soruşturma takipsizlikle kapandı",
            },
        ]).to_excel(w, sheet_name="Olay_Defteri", index=False)
        pd.DataFrame([
            {
                "Şirket": 101, "Şirket_Adi": "Örnek İnşaat A.Ş.",
                "Birlesik_Seviye": "Kırmızı Alarm (Stage 3 Sinyali)", "Birlesik_Skor": 80.0,
                "Risk_Matrisi": "Bilanço Kırılgan × Kritik Haber Şoku", "Toplam_Bakiye_TRY": 250_000_000.0,
                "Haber_Sinyali": "Kırmızı Alarm (Stage 3 Sinyali)", "Haber_Skoru": 80.0,
                "Finansal_Seviye": "Yakın İzleme (Stage 2)", "Finansal_Skor": 62.0,
            }
        ]).to_excel(w, sheet_name="Birlesik", index=False)

    server = mcp_server.create_mcp_server(db=db, oracle_conn=None, ews_excel_path=str(ews_xlsx))

    async def _verify_mcp():
        tools = await server.list_tools()
        tool_names = {t.name for t in tools}
        for req in ("bilgi_ara", "tablo_karti", "join_yolu", "koken", "son_donem",
                    "sql_calistir", "ews_olay_defteri", "ews_sirket_ozeti"):
            assert req in tool_names, f"MCP aracı eksik: {req}"

        def _text(res):
            if hasattr(res, "content"):
                return "\n".join(getattr(c, "text", str(c)) for c in res.content)
            return str(res)

        r_ara = _text(await server.call_tool("bilgi_ara", {"kelimeler": ["LOANS"]}))
        assert "DWH.LOANS" in r_ara

        r_kart = _text(await server.call_tool("tablo_karti", {"tablo": "DWH.LOANS"}))
        assert "CUST_ID" in r_kart

        r_join = _text(await server.call_tool("join_yolu", {"tablo1": "DWH.LOANS", "tablo2": "DWH.CUSTOMERS"}))
        assert "DWH.LOANS.CUST_ID = DWH.CUSTOMERS.CUST_ID" in r_join

        r_kok = _text(await server.call_tool("koken", {"tablo": "DWH.LOANS"}))
        assert "DWH.CUSTOMERS" in r_kok

        r_don = _text(await server.call_tool("son_donem", {"ad": "PD_MAPPING"}))
        assert "PD_MAPPING_202604" in r_don

        r_sql = _text(await server.call_tool("sql_calistir", {"sql": "SELECT 1 FROM DUAL", "amac": "test"}))
        assert "Oracle bağlantısı yok" in r_sql

        r_ews = _text(await server.call_tool("ews_olay_defteri", {"sirket": "örnek inşaat", "sadece_acik": True}))
        assert "konkordato_iflas" in r_ews and "Beta Enerji" not in r_ews

        r_ozet = _text(await server.call_tool("ews_sirket_ozeti", {"sirket": "101"}))
        assert "Bilanço Kırılgan" in r_ozet and "konkordato_iflas" in r_ozet

    asyncio.run(_verify_mcp())

