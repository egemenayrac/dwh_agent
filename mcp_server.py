# -*- coding: utf-8 -*-
"""
mcp_server.py - DWH Ajanı ve EWS Olay Defteri için standart MCP (Model Context Protocol) Sunucusu.

FastMCP / resmi `mcp` SDK üzerinden şu araçları standart MCP araçları olarak açar:
  - DWH Bilgi Tabanı ve Oracle araçları:
      bilgi_ara, tablo_karti, join_yolu, koken, son_donem, sql_calistir
  - EWS (news_ingestion) Olay Defteri köprüsü:
      ews_olay_defteri, ews_sirket_ozeti
  - MCP Kaynakları (Resources):
      dwh://kb/stats, dwh://kb/onayli-bilgiler

Çalıştırma (stdio):
    python mcp_server.py
Ortam değişkenleri:
    DWH_KB_PATH   : kb.sqlite dosya yolu (varsayılan: ajan_core.KB_PATH)
    EWS_DIR       : news_ingestion çıktı klasörü (llm_dogrulama.xlsx burada aranır)
    EWS_EXCEL_PATH: llm_dogrulama.xlsx tam yolu (opsiyonel)

Python 3.9+ uyumludur (`from __future__ import annotations`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ajan_core as core  # noqa: E402
import schemas  # noqa: E402

try:
    from fastmcp import FastMCP
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x
    except ImportError:
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]


def _resolve_ews_excel(ews_dir: Optional[str] = None, ews_excel_path: Optional[str] = None) -> Optional[Path]:
    """EWS llm_dogrulama.xlsx dosyasını (en güncel tarihli sürüm dahil) bulur."""
    if ews_excel_path:
        p = Path(ews_excel_path)
        return p if p.exists() else None
    env_excel = os.getenv("EWS_EXCEL_PATH")
    if env_excel:
        p = Path(env_excel)
        if p.exists():
            return p

    candidates: List[Path] = []
    if ews_dir:
        candidates.append(Path(ews_dir))
    if os.getenv("EWS_DIR"):
        candidates.append(Path(os.environ["EWS_DIR"]))
    if os.getenv("NEWS_INGESTION_DIR"):
        candidates.append(Path(os.environ["NEWS_INGESTION_DIR"]))
    ayar_ews = getattr(core.kb, "EWS_DIR", None)
    if ayar_ews:
        candidates.append(Path(ayar_ews))
    # Varsayılan komşu repo konumu: ../news_ingestion
    candidates.append(Path(__file__).resolve().parent.parent / "news_ingestion")

    for base in candidates:
        if not base.exists():
            continue
        direct = base / "llm_dogrulama.xlsx"
        if direct.exists():
            return direct
        stamped = sorted(base.glob("llm_dogrulama*.xlsx"), key=lambda x: x.stat().st_mtime, reverse=True)
        if stamped:
            return stamped[0]
    return None


def _read_ews_sheet(excel_path: Path, sheet_name: str) -> pd.DataFrame:
    with pd.ExcelFile(excel_path) as book:
        if sheet_name not in book.sheet_names:
            return pd.DataFrame()
        return pd.read_excel(book, sheet_name=sheet_name)


def _format_ews_ledger(
    excel_path: Optional[Path],
    sirket: str = "",
    sadece_acik: bool = False,
    min_seviye: str = "",
    limit: int = 25,
) -> str:
    """EWS Olay_Defteri sayfasını filtreleyip okunabilir metin özeti olarak döner."""
    if excel_path is None or not excel_path.exists():
        return (
            "EWS çıktısı (llm_dogrulama.xlsx) bulunamadı. "
            "EWS_DIR veya EWS_EXCEL_PATH ortam değişkenini ayarlayın ya da news_ingestion/3_llm_dogrulama.py çalıştırın."
        )
    df = _read_ews_sheet(excel_path, "Olay_Defteri")
    if df.empty:
        return f"EWS Olay Defteri boş ({excel_path.name})."

    filtered = df.copy()
    if sirket and sirket.strip():
        q = core.tr_fold(sirket.strip())
        name_col = filtered["Şirket_Adi"].fillna("").map(core.tr_fold) if "Şirket_Adi" in filtered.columns else ""
        id_col = filtered["Şirket"].fillna("").map(core.tr_fold) if "Şirket" in filtered.columns else ""
        mask = name_col.str.contains(q, regex=False) | id_col.str.contains(q, regex=False)
        filtered = filtered.loc[mask]

    if sadece_acik and "Durum" in filtered.columns:
        durum_fold = filtered["Durum"].fillna("").map(core.tr_fold)
        filtered = filtered.loc[durum_fold.str.startswith("acik")]

    sev_rank = {"CRITICAL": 3, "HIGH": 2, "MODERATE": 1}
    min_key = (min_seviye or "").strip().upper()
    if min_key in sev_rank and "Seviye" in filtered.columns:
        min_r = sev_rank[min_key]
        filtered = filtered.loc[filtered["Seviye"].fillna("").str.upper().map(lambda s: sev_rank.get(s, 0)) >= min_r]

    if filtered.empty:
        return f"Filtreye uyan EWS olayı bulunamadı (sirket={sirket!r}, sadece_acik={sadece_acik}, min_seviye={min_seviye!r})."

    cap = max(1, min(int(limit), 100))
    shown = filtered.head(cap)
    lines = [f"EWS OLAY DEFTERİ ({len(shown)}/{len(filtered)} olay, kaynak: {excel_path.name}):"]
    for _, row in shown.iterrows():
        sirket_id = row.get("Şirket", "")
        sirket_ad = row.get("Şirket_Adi", "")
        kod = row.get("Olay", "")
        tip = row.get("Olay_Tipi", "")
        seviye = row.get("Seviye", "")
        durum = row.get("Durum", "")
        etki = row.get("Sinyale_Etkisi", "")
        son_haber = row.get("Son_Haber", "")
        tanim = core.short(row.get("Tanim", ""), 180)
        lines.append(
            f"- [{kod}] {sirket_ad} (ID: {sirket_id}) | {tip} ({seviye}) | {durum} | {etki} | Son haber: {son_haber}\n"
            f"  Özet: {tanim}"
        )
    return "\n".join(lines)


def _format_ews_company_summary(excel_path: Optional[Path], sirket: str) -> str:
    """EWS Birlesik + Şirketler + Olay_Defteri verilerini tek şirket kartında birleştirir."""
    if excel_path is None or not excel_path.exists():
        return "EWS çıktısı (llm_dogrulama.xlsx) bulunamadı. EWS_DIR veya EWS_EXCEL_PATH ayarlayın."
    q = core.tr_fold((sirket or "").strip())
    if not q:
        return "Aranacak şirket adı veya müşteri numarası (counterparty_id) verin."

    out: List[str] = []
    for sheet in ("Birlesik", "Şirketler"):
        df = _read_ews_sheet(excel_path, sheet)
        if df.empty:
            continue
        name_col = df["Şirket_Adi"].fillna("").map(core.tr_fold) if "Şirket_Adi" in df.columns else pd.Series("", index=df.index)
        id_col = df["Şirket"].fillna("").map(core.tr_fold) if "Şirket" in df.columns else pd.Series("", index=df.index)
        hit = df.loc[name_col.str.contains(q, regex=False) | id_col.str.contains(q, regex=False)]
        if not hit.empty:
            row = hit.iloc[0]
            out.append(f"EWS ŞİRKET ÖZETİ ({sheet} — {row.get('Şirket_Adi')} / ID: {row.get('Şirket')}):")
            for col in (
                "Birlesik_Seviye",
                "Birlesik_Skor",
                "Risk_Matrisi",
                "Toplam_Bakiye_TRY",
                "Haber_Sinyali",
                "Haber_Skoru",
                "LLM_Sinyali",
                "Finansal_Seviye",
                "Finansal_Skor",
                "DSCR_Degeri",
                "Risk_Olaylari",
                "En_Agir_Olay",
                "Not",
            ):
                if col in row.index and pd.notna(row[col]) and str(row[col]).strip():
                    out.append(f"- {col}: {row[col]}")
            break

    ledger_txt = _format_ews_ledger(excel_path, sirket=sirket, limit=10)
    if out:
        out.append("\n" + ledger_txt)
        return "\n".join(out)
    return ledger_txt


def create_mcp_server(
    db: Optional[sqlite3.Connection] = None,
    kb_path: Optional[str] = None,
    oracle_conn: Optional[Any] = None,
    ews_dir: Optional[str] = None,
    ews_excel_path: Optional[str] = None,
    name: str = "dwh-agent-mcp",
) -> Any:
    """DWH araçları ve EWS olay defteri köprüsünü içeren FastMCP sunucusu oluşturur."""
    server = FastMCP(name)
    state: dict = {"db": db, "conn": oracle_conn}

    def _get_db() -> sqlite3.Connection:
        if state["db"] is not None:
            return state["db"]
        path = kb_path or os.getenv("DWH_KB_PATH") or core.KB_PATH
        conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        state["db"] = core.init_db(conn)
        return state["db"]

    def _ctx(use_verified: bool = True) -> core.Ctx:
        return core.Ctx(_get_db(), conn=state["conn"], use_verified=use_verified)

    @server.tool
    def bilgi_ara(kelimeler: List[str], use_verified: bool = True) -> str:
        """Bilgi tabanında (kb.sqlite) tablo, kart, onaylı sorgu, geçmiş SQL ve dönemsel aile arar."""
        parsed = schemas.BilgiAraInput(kelimeler=kelimeler)
        return _ctx(use_verified=use_verified).bilgi_ara(parsed.kelimeler)

    @server.tool
    def tablo_karti(tablo: str) -> str:
        """Bir tablonun kartını (özet, satır seviyesi, kolon profilleri, LOOKUP kodları, join'ler) döner."""
        parsed = schemas.TabloKartiInput(tablo=tablo)
        return _ctx().tablo_karti(parsed.tablo)

    @server.tool
    def join_yolu(tablo1: str, tablo2: str) -> str:
        """İki tablo arasındaki doğrulanmış en kısa join yolunu ve ON koşullarını verir."""
        parsed = schemas.JoinYoluInput(tablo1=tablo1, tablo2=tablo2)
        return _ctx().join_yolu(parsed.tablo1, parsed.tablo2)

    @server.tool
    def koken(tablo: str) -> str:
        """Tablonun yukarı (kaynak) ve aşağı (hedef) veri kökeni (lineage) bilgisini döner."""
        parsed = schemas.KokenInput(tablo=tablo)
        return _ctx().koken(parsed.tablo)

    @server.tool
    def son_donem(ad: str) -> str:
        """Dönemsel tablo ailesinin (ör. PD_MAPPING, SME_WL) dönem sayısını ve en yeni tablosunu verir."""
        parsed = schemas.SonDonemInput(ad=ad)
        return _ctx().son_donem(parsed.ad)

    @server.tool
    def sql_calistir(sql: str, amac: str = "", zorla: bool = False) -> str:
        """Oracle üzerinde yalnızca salt-okunur SELECT/WITH sorgusu çalıştırır (EXPLAIN PLAN korumalı)."""
        parsed = schemas.SqlCalistirInput(sql=sql, amac=amac, zorla=zorla)
        return _ctx().sql_calistir(parsed.sql, amac=parsed.amac, zorla=parsed.zorla)

    @server.tool
    def ews_olay_defteri(
        sirket: str = "",
        sadece_acik: bool = False,
        min_seviye: str = "",
        limit: int = 25,
    ) -> str:
        """EWS (news_ingestion) Olay Defteri köprüsü: portföy şirketlerinin açık/kapalı kredi riski olaylarını döner."""
        excel = _resolve_ews_excel(ews_dir=ews_dir, ews_excel_path=ews_excel_path)
        return _format_ews_ledger(excel, sirket=sirket, sadece_acik=sadece_acik, min_seviye=min_seviye, limit=limit)

    @server.tool
    def ews_sirket_ozeti(sirket: str) -> str:
        """EWS Birlesik + Olay_Defteri köprüsü: şirket bazında haber sinyali, finansal skor ve risk matrisini döner."""
        excel = _resolve_ews_excel(ews_dir=ews_dir, ews_excel_path=ews_excel_path)
        return _format_ews_company_summary(excel, sirket=sirket)

    @server.resource("dwh://kb/stats")
    def kb_stats_resource() -> str:
        """DWH bilgi tabanı istatistik özeti (JSON)."""
        return json.dumps(core.kb_stats(_get_db()), ensure_ascii=False, indent=2)

    @server.resource("dwh://kb/onayli-bilgiler")
    def onayli_bilgiler_resource() -> str:
        """Ekibin onayladığı kesin iş kuralları (onayli_bilgiler.txt)."""
        return core.onayli_bilgiler() or "(onayli_bilgiler.txt boş veya tanımlı değil)"

    return server


mcp = create_mcp_server()


if __name__ == "__main__":
    mcp.run()
