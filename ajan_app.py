# -*- coding: utf-8 -*-
"""
ajan_app.py - Veri ambarı ajanı, Streamlit arayüzü

Başlatma: Spyder'da baslat.py'yi F5 ile çalıştır (tarayıcıda http://localhost:8501 açılır).
Mantık ajan_core.py'de; bu dosya sadece ekran.
"""
from __future__ import annotations

import html
import io
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ajan_core as core  # noqa: E402

try:
    import agent_graph as graph_engine  # noqa: E402
except ImportError:
    graph_engine = None

st.set_page_config(page_title="Veri Ambarı Ajanı", page_icon="📊", layout="wide")


@st.cache_data(ttl=600)
def get_stats():
    conn = core.open_kb()
    try:
        return core.kb_stats(conn)
    finally:
        conn.close()


ss = st.session_state
if "db" not in ss:
    ss.db = core.open_kb()
db = ss.db
ss.setdefault("conn", None)
ss.setdefault("turns", [])        # her soru: {"sess", "ctx", "fb", "full_xlsx"}
ss.setdefault("mail_turn", None)

# ------------------------------- Sol panel -------------------------------
with st.sidebar:
    st.header("Bağlantı")
    if not core.get_api_key():
        st.warning(f"LLM API anahtarı gerekli ({core.kb.API_KEY_PAGE or 'anahtar sayfası'}).")
        key = st.text_input("IBThink API anahtarı", type="password")
        if st.button("Anahtarı kaydet") and key.strip():
            core.save_api_key(key)
            st.rerun()
    else:
        st.caption("IBThink anahtarı: tanımlı")
    if ss.conn is None:
        user = st.text_input("Oracle kullanıcı", value=getattr(core.kb, "DB_USER", ""))
        pwd = st.text_input("Şifre", type="password")
        if st.button("Bağlan", type="primary"):
            try:
                ss.conn = core.oracle_connect(user, pwd)
                st.rerun()
            except Exception as e:
                st.error(f"Bağlanılamadı: {str(e)[:200]}")
        st.caption("Bağlanmadan da tablo/kart soruları sorulabilir; SQL çalıştırmak için bağlantı gerekir.")
    else:
        st.success("Oracle bağlı")
        if st.button("Bağlantıyı kapat"):
            try:
                ss.conn.close()
            except Exception:
                pass
            ss.conn = None
            st.rerun()
    st.divider()
    stats = get_stats()
    st.caption(f"Bilgi tabanı: {stats['tablo']:,} tablo · {stats['kullanilan']:,} kullanılan · "
               f"{stats['join']} join · {stats['kart']} kart · {stats['onayli_sorgu']} onaylı sorgu")
    use_langgraph = False
    if graph_engine is not None:
        use_langgraph = st.checkbox("LangGraph akış motoru (HITL + Checkpointer)", value=True)
        with st.expander("Akış diyagramı (Mermaid)"):
            st.code(graph_engine.get_mermaid_diagram(), language="mermaid")
    if st.button("Yeni sohbet"):
        ss.turns = []
        st.rerun()


def _new_session(question, extra_system=""):
    if use_langgraph and graph_engine is not None:
        return graph_engine.GraphSession(question, extra_system=extra_system)
    return core.Session(question, extra_system=extra_system)


def _run_session(sess, ctx, on_step=None):
    if graph_engine is not None and isinstance(sess, graph_engine.GraphSession):
        return graph_engine.run_graph(sess, ctx, on_step=on_step)
    return core.run(sess, ctx, on_step=on_step)


def _resume_session(sess, ctx, answer, on_step=None):
    if graph_engine is not None and isinstance(sess, graph_engine.GraphSession):
        return graph_engine.resume_graph(sess, ctx, answer, on_step=on_step)
    return core.resume(sess, ctx, answer, on_step=on_step)


# ------------------------------- Yardımcılar -------------------------------
def show_steps(sess, expanded=False):
    if not sess.steps:
        return
    with st.expander(f"Adımlar ({len(sess.steps)} araç çağrısı, {sess.n_llm} LLM isteği)", expanded=expanded):
        for i, s in enumerate(sess.steps, 1):
            st.markdown(f"**{i}. {s['arac']}**")
            if s["girdi"]:
                if s["arac"] in ("sql_calistir", "tekillik_kontrol") and '"sql"' in s["girdi"]:
                    try:
                        import json
                        st.code(json.loads(s["girdi"]).get("sql", ""), language="sql")
                    except Exception:
                        st.code(s["girdi"])
                else:
                    st.caption(s["girdi"][:600])
            # st.text uzun satırları alt satıra geçirmiyordu (sağda kesik görünüyordu); kaydırmalı gösterim:
            st.markdown("<div style='white-space:pre-wrap;font-family:monospace;font-size:0.85em'>"
                        + html.escape(s["cikti"][:3000]) + "</div>", unsafe_allow_html=True)


def live_runner(status_box):
    def on_step(step):
        status_box.write(f"🔧 {step['arac']}: {step['girdi'][:120]}")
    return on_step


def show_result(turn, key):
    sess, ctx = turn["sess"], turn["ctx"]
    if sess.status == "error":
        st.error(f"Hata: {sess.error}")
        return
    if sess.status == "waiting":
        p = sess.pending
        title = {"plan_onayla": "📋 Plan onayı", "tablo_olustur": "🛠️ Tablo oluşturma onayı"}.get(
            p["kind"], "❓ Ajanın sorusu")
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.markdown(p["text"])
            ans = st.text_area("Cevabın / düzeltmen (boş bırakıp Onayla'ya basabilirsin)", key=f"ans{key}")
            c1, c2 = st.columns(2)
            go = None
            if c1.button("✅ Onayla", key=f"ok{key}", type="primary"):
                go = "Onaylandı, devam et." + (f" Not: {ans}" if ans.strip() else "")
            if c2.button("✏️ Düzeltmeyi gönder", key=f"fix{key}") and ans.strip():
                go = ans.strip()
            if go:
                with st.status("Devam ediyor...", expanded=True) as box:
                    _resume_session(sess, ctx, go, on_step=live_runner(box))
                st.rerun()
        return
    r = sess.result or {}
    st.markdown(r.get("cevap", ""))
    if r.get("varsayimlar"):
        st.caption(f"Varsayımlar: {r['varsayimlar']}")
    if r.get("sql"):
        with st.expander("SQL"):
            st.code(r["sql"], language="sql")
    df = ctx.last_df
    if df is not None:
        st.dataframe(df, height=min(400, 40 + 35 * min(len(df), 10)))
        if ctx.last_more:
            st.caption(f"Ekranda ilk {core.SHOW_ROWS:,} satır gösteriliyor.")
        c1, c2 = st.columns(2)
        c1.download_button("📥 Excel indir (ekrandaki sonuç)", core.to_excel(df, sess.question, r.get("sql", "")),
                           file_name="sonuc.xlsx", key=f"x{key}")
        if ctx.last_more and ss.conn is not None:
            if turn.get("full_xlsx") is None:
                if c2.button(f"Tüm sonucu çek (en fazla {core.EXPORT_MAX_ROWS:,} satır)", key=f"full{key}"):
                    with st.spinner("Oracle'dan tüm sonuç çekiliyor..."):
                        full, more = core.export_full(ss.conn, ctx.last_sql)
                        turn["full_xlsx"] = core.to_excel(full, sess.question, ctx.last_sql)
                    st.rerun()
            else:
                c2.download_button("📥 Tüm sonucu Excel indir", turn["full_xlsx"], file_name="sonuc_tam.xlsx",
                                   key=f"xf{key}")
    show_steps(sess)
    if r.get("sql") and turn.get("fb") is None:
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("👍 Doğru", key=f"good{key}"):
            core.save_verified(db, sess.question, r["sql"], r.get("varsayimlar", ""))
            turn["fb"] = "dogru"
            st.rerun()
        if c2.button("👎 Yanlış", key=f"bad{key}"):
            turn["fb"] = "yanlis_not"
            st.rerun()
    elif turn.get("fb") == "yanlis_not":
        note = st.text_input("Neyi yanlıştı? (isteğe bağlı — iyileştirme için kaydedilir)", key=f"fbnote{key}")
        c1, c2, _ = st.columns([1.4, 1, 3.6])
        if c1.button("Geri bildirimi kaydet", key=f"fbsave{key}", type="primary"):
            core.save_feedback(db, sess.question, r.get("sql", ""), "yanlis", note)
            turn["fb"] = "yanlis"
            st.rerun()
        if c2.button("Vazgeç", key=f"fbcancel{key}"):
            turn["fb"] = None
            st.rerun()
    elif turn.get("fb") == "dogru":
        st.caption("✓ Onaylı sorgu kütüphanesine eklendi.")
    elif turn.get("fb") == "yanlis":
        st.caption("✓ Geri bildirim kaydedildi. Neyin yanlış olduğunu yeni bir mesajla yazarsan ajan düzeltmeyi dener.")


# ------------------------------- Sekmeler -------------------------------
tab_soru, tab_mail, tab_olcum = st.tabs(["💬 Soru", "✉️ Mail talebi", "📏 Ölçüm"])

with tab_soru:
    for i, turn in enumerate(ss.turns):
        with st.chat_message("user"):
            st.write(turn["sess"].question)
        with st.chat_message("assistant"):
            show_result(turn, i)
    waiting = any(t["sess"].status == "waiting" for t in ss.turns)
    q = st.chat_input("Sorunu yaz (ör. Son 3 ayda 90+ gecikmeye düşen nakit kredi sayısı, aylık)", disabled=waiting)
    if q:
        prev = [t for t in ss.turns if t["sess"].status == "done"]
        extra = ""
        if prev:  # bir önceki sorunun özeti taşınır (tam geçmiş değil)
            p = prev[-1]["sess"]
            extra = (f"\n\nÖNCEKİ SORU: {p.question}\nÖNCEKİ CEVAP: {core.short((p.result or {}).get('cevap'), 600)}\n"
                     f"ÖNCEKİ SQL: {core.short((p.result or {}).get('sql'), 1500)}")
        sess, ctx = _new_session(q, extra_system=extra), core.Ctx(db, ss.conn)
        ss.turns.append({"sess": sess, "ctx": ctx, "fb": None, "full_xlsx": None})
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            with st.status("Çalışıyor...", expanded=True) as box:
                _run_session(sess, ctx, on_step=live_runner(box))
                box.update(label="Tamamlandı" if sess.status == "done" else
                           ("Cevabın bekleniyor" if sess.status == "waiting" else "Hata"), state="complete")
        st.rerun()
    if waiting:
        st.info("Ajan senden onay ya da cevap bekliyor (yukarıda).")

with tab_mail:
    st.markdown("Talep mailinin metnini yapıştır. Ajan içindeki SQL'i kontrol edip çalıştırır ve cevap taslağı "
                "hazırlar. **Göndermeden önce sonucu ve taslağı kontrol et.**")
    mail = st.text_area("Mail metni", height=220, key="mail_text")
    if st.button("İşle", type="primary", disabled=not mail.strip()):
        sess, ctx = _new_session(core.MAIL_PROMPT + mail), core.Ctx(db, ss.conn)
        sess.question = "Mail talebi: " + core.short(mail.strip().splitlines()[0] if mail.strip() else "", 80)
        ss.mail_turn = {"sess": sess, "ctx": ctx, "fb": None, "full_xlsx": None}
        with st.status("Çalışıyor...", expanded=True) as box:
            _run_session(sess, ctx, on_step=live_runner(box))
        st.rerun()
    if ss.mail_turn:
        st.divider()
        st.markdown("**Cevap taslağı ve sonuç**")
        show_result(ss.mail_turn, "mail")

with tab_olcum:
    st.markdown(
        "**Ölçüm ne yapar?** Ekibin gerçek sorularından ve her birinin **doğru SQL**'inden oluşan bir set yüklersin. "
        "Ajan her soruyu onay sormadan çözer, sonucu doğru SQL'in sonucuyla karşılaştırır:\n"
        "- **DOĞRU**: aynı satırlar (satır/kolon sırası ve kolon adları önemsiz, fazladan kolon serbest)\n"
        "- **İNCELE**: neredeyse aynı (hücrelerin %90+'ı örtüşüyor), insan bakmalı\n"
        "- **YANLIŞ** / **HATA**: farklı sonuç ya da sonuç yok\n\n"
        "Ayrıca tablo isabeti (doğru tabloları seçti mi), adım sayısı ve süre ölçülür. Her ölçüm kaydedilir; "
        "bir değişiklikten (yeni kartlar, prompt) sonra tekrar çalıştırıp iyileşmeyi görebilirsin.")
    tmpl = io.BytesIO()
    pd.DataFrame({"soru": ["Ekim 2026 sonu 90+ gecikmedeki nakit kredi sayısı"],
                  "dogru_sql": ["SELECT COUNT(*) FROM ..."], "kategori": ["gecikme"]}).to_excel(tmpl, index=False)
    ver_bytes, ver_count = core.export_verified_excel(db)
    b1, b2 = st.columns(2)
    b1.download_button("📄 Boş şablonu indir", tmpl.getvalue(), file_name="olcum_seti_sablon.xlsx")
    b2.download_button(f"📥 Onaylı sorguları dışa aktar ({ver_count} sorgu)", ver_bytes,
                       file_name="olcum_seti.xlsx", disabled=ver_count == 0)
    up = st.file_uploader("Ölçüm seti (soru, dogru_sql, isteğe bağlı kategori)", type=["xlsx", "csv"])
    if up is not None:
        data = pd.read_excel(up) if up.name.endswith("xlsx") else pd.read_csv(up, sep=";")
        data.columns = [str(c).strip().lower() for c in data.columns]
        if not {"soru", "dogru_sql"} <= set(data.columns):
            st.error("Dosyada 'soru' ve 'dogru_sql' kolonları olmalı.")
        else:
            st.write(f"{len(data)} soru. Tahmini süre: soru başına 1-3 dakika.")
            disable_ver = st.checkbox(
                "Ölçüm sırasında onaylı sorgu hafızasını kapat (önerilen: ezber sızıntısını önler)", value=True)
            if ss.conn is None:
                st.warning("Ölçüm için Oracle bağlantısı gerekli.")
            elif st.button("Ölçümü başlat", type="primary"):
                bar = st.progress(0.0)
                rows = list(zip(data["soru"], data["dogru_sql"],
                                data["kategori"] if "kategori" in data else [""] * len(data)))
                run_id, res = core.evaluate(
                    db, ss.conn, rows,
                    on_progress=lambda i, n, r: bar.progress(
                        i / n, text=f"{i}/{n}: {r['sonuc']} — {core.short(r['soru'], 60)}"),
                    use_verified=not disable_ver,
                )
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Doğru", f"{(res['sonuc'] == 'DOĞRU').sum()} / {len(res)}")
                c2.metric("İncele", int((res["sonuc"] == "İNCELE").sum()))
                c3.metric("Ort. tablo isabeti", f"{res['tablo_isabet'].dropna().mean():.0%}"
                          if res["tablo_isabet"].notna().any() else "-")
                c4.metric("Ort. süre", f"{res['sure_sn'].mean():.0f} sn")
                if res["kategori"].astype(bool).any():
                    st.dataframe(res.groupby("kategori")["sonuc"].value_counts().unstack(fill_value=0))
                st.dataframe(res)
                buf = io.BytesIO()
                res.to_excel(buf, index=False)
                st.download_button("📥 Ölçüm sonucunu indir", buf.getvalue(), file_name=f"olcum_{run_id}.xlsx")
    hist = core.eval_history(db)
    if len(hist):
        st.markdown("**Önceki ölçümler** (iyileşmeyi buradan takip et)")
        st.dataframe(hist)
    fb_neg = core.list_feedback(db, sonuc="yanlis", limit=30)
    if len(fb_neg):
        with st.expander(f"👎 Kayıtlı hatalı cevap geri bildirimleri ({len(fb_neg)})"):
            st.dataframe(fb_neg)
