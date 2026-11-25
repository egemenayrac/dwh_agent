# -*- coding: utf-8 -*-
"""
schemas.py - dwh_agent için Pydantic v2 araç, çıktı ve kart şemaları.

Tüm ajan araçlarının giriş/çıkış modelleri burada tek kaynak olarak tanımlanır.
OpenAI uyumlu TOOLS listesi Pydantic v2'nin `model_json_schema()` çıktısından otomatik üretilir;
böylece şema tanımı ile çalışma zamanı doğrulaması her zaman birebir örtüşür.

Python 3.9+ uyumludur (`from __future__ import annotations` + `typing` tipleri).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


# =============================================================================
# 1. Araç Giriş Şemaları (Tool Input Models)
# =============================================================================
class BilgiAraInput(BaseModel):
    """bilgi_ara aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    kelimeler: List[str] = Field(
        ...,
        min_length=1,
        description="Aranacak Türkçe/İngilizce terimler ve kısaltmalar (ör. ['gecikme', 'delay', 'dpd', 'dlq']).",
    )

    @field_validator("kelimeler", mode="before")
    @classmethod
    def _coerce_kelimeler(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


class TabloKartiInput(BaseModel):
    """tablo_karti aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    tablo: str = Field(..., min_length=1, description="OWNER.TABLO biçiminde tablo adı.")


class JoinYoluInput(BaseModel):
    """join_yolu aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    tablo1: str = Field(..., min_length=1, description="İlk tablo adı (OWNER.TABLO).")
    tablo2: str = Field(..., min_length=1, description="İkinci tablo adı (OWNER.TABLO).")


class KokenInput(BaseModel):
    """koken aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    tablo: str = Field(..., min_length=1, description="Kökeni sorgulanacak tablo (OWNER.TABLO).")


class SonDonemInput(BaseModel):
    """son_donem aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    ad: str = Field(..., min_length=1, description="Dönemsel tablo ailesi adı (ör. PD_MAPPING, SME_WL).")


class SqlCalistirInput(BaseModel):
    """sql_calistir aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    sql: str = Field(..., min_length=1, description="Oracle'da çalıştırılacak tek SELECT/WITH sorgusu.")
    amac: str = Field(..., description="Sorgunun kısa amacı.")
    zorla: bool = Field(
        default=False,
        description="Ağır sorgu uyarısını kullanıcı onayı sonrasında aşmak için true verilir.",
    )


class TekillikKontrolInput(BaseModel):
    """tekillik_kontrol aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    sql: str = Field(..., min_length=1, description="Tekilliği kontrol edilecek SELECT sorgusu.")
    anahtar_kolonlar: List[str] = Field(
        ...,
        min_length=1,
        description="Tekil olması beklenen anahtar kolon adları.",
    )

    @field_validator("anahtar_kolonlar", mode="before")
    @classmethod
    def _coerce_keys(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v


class PlanOnaylaInput(BaseModel):
    """plan_onayla aracı giriş şeması (Human-in-the-Loop)."""

    model_config = ConfigDict(extra="ignore")

    plan: str = Field(..., min_length=1, description="Kullanıcıya sunulacak 3-6 maddelik çözüm planı.")


class KullaniciyaSorInput(BaseModel):
    """kullaniciya_sor aracı giriş şeması (Human-in-the-Loop)."""

    model_config = ConfigDict(extra="ignore")

    soru: str = Field(..., min_length=1, description="Kullanıcıya sorulacak net soru.")


class TabloOlusturInput(BaseModel):
    """tablo_olustur aracı giriş şeması (kullanıcı onaylı CTAS)."""

    model_config = ConfigDict(extra="ignore")

    tablo_adi: str = Field(..., min_length=1, description="Kullanıcının kendi şemasında oluşturulacak yeni tablo adı.")
    sql: str = Field(..., min_length=1, description="Tabloyu dolduracak SELECT/WITH sorgusu.")
    aciklama: str = Field(default="", description="Tablonun kısa açıklaması.")
    zorla: bool = Field(default=False, description="Ağır sorgu uyarısını kullanıcı onayı ile aşmak için true.")


class CevapVerInput(BaseModel):
    """cevap_ver aracı giriş şeması."""

    model_config = ConfigDict(extra="ignore")

    cevap: str = Field(..., description="Kullanıcıya verilecek 3-10 satırlık Türkçe nihai cevap.")
    sql: str = Field(default="", description="Kullanılan nihai SQL (yoksa boş).")
    varsayimlar: str = Field(default="", description="Varsayımlar ve önemli notlar.")


# =============================================================================
# 2. Araç ve Ajan Çıkış Şemaları (Output Models)
# =============================================================================
class ToolStep(BaseModel):
    """Tek bir araç çağrısının izlenebilir adım kaydı."""

    arac: str
    girdi: str = ""
    cikti: str = ""


class AgentResult(BaseModel):
    """Ajanın nihai yapılandırılmış cevabı."""

    cevap: str = ""
    sql: str = ""
    varsayimlar: str = ""


class ToolExecutionOutput(BaseModel):
    """Standart araç çalıştırma sonucu."""

    arac: str
    basarili: bool = True
    cikti: str = ""
    hata: Optional[str] = None


class SqlCalistirOutput(BaseModel):
    """sql_calistir yapılandırılmış özet çıktısı."""

    basarili: bool = True
    satir_sayisi: int = 0
    devami_var: bool = False
    kolonlar: List[str] = Field(default_factory=list)
    sure_sn: float = 0.0
    uyari: Optional[str] = None
    hata: Optional[str] = None
    metin: str = ""


# =============================================================================
# 3. Bilgi Tabanı LLM Kart Şemaları (kb_annotate.py)
# =============================================================================
def _to_str_list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return [str(v)]


class KolonAnlam(BaseModel):
    """Tablo kartındaki tarih/önemli kolon açıklaması."""

    model_config = ConfigDict(extra="ignore")

    kolon: str = ""
    anlam: str = ""


class DosyaAdimi(BaseModel):
    """Dosya kartındaki işlem adımı."""

    model_config = ConfigDict(extra="ignore")

    sira: int = 1
    ne_yapar: str = ""
    olusturdugu_tablo: Optional[str] = None
    kullandigi_tablolar: List[str] = Field(default_factory=list)

    @field_validator("sira", mode="before")
    @classmethod
    def _coerce_sira(cls, v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 1

    @field_validator("kullandigi_tablolar", mode="before")
    @classmethod
    def _coerce_tabs(cls, v: Any) -> List[str]:
        return _to_str_list(v)


class DosyaParametresi(BaseModel):
    """Dosya kartındaki parametre tanımı."""

    model_config = ConfigDict(extra="ignore")

    ad: str = ""
    aciklama: str = ""
    ornek: Optional[str] = None


class FileCardSchema(BaseModel):
    """kb_annotate.py dosya kartı LLM JSON şeması."""

    model_config = ConfigDict(extra="ignore")

    amac: str = ""
    is_alani: str = ""
    cikti: str = ""
    adimlar: List[DosyaAdimi] = Field(default_factory=list)
    girdi_tablolari: List[str] = Field(default_factory=list)
    parametreler: List[DosyaParametresi] = Field(default_factory=list)
    is_kurallari: List[str] = Field(default_factory=list)
    donemsel: bool = False
    durum: Literal["uretim", "deneme", "eski", "belirsiz"] = "belirsiz"
    durum_gerekcesi: str = ""
    tekrar_kullanilabilir: bool = False
    anahtar_kelimeler: List[str] = Field(default_factory=list)

    @field_validator("durum", mode="before")
    @classmethod
    def _norm_durum(cls, v: Any) -> str:
        val = str(v or "").strip().lower().replace("ü", "u")
        return val if val in ("uretim", "deneme", "eski", "belirsiz") else "belirsiz"

    @field_validator("girdi_tablolari", "is_kurallari", "anahtar_kelimeler", mode="before")
    @classmethod
    def _norm_lists(cls, v: Any) -> List[str]:
        return _to_str_list(v)

    @field_validator("adimlar", mode="before")
    @classmethod
    def _norm_adimlar(cls, v: Any) -> List[Any]:
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict)]

    @field_validator("parametreler", mode="before")
    @classmethod
    def _norm_parametreler(cls, v: Any) -> List[Any]:
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict)]


class TableCardSchema(BaseModel):
    """kb_annotate.py tablo kartı LLM JSON şeması."""

    model_config = ConfigDict(extra="ignore")

    ozet: str = ""
    is_alani: str = ""
    satir_seviyesi: str = ""
    anahtar_kolonlar: List[str] = Field(default_factory=list)
    tarih_kolonlari: List[KolonAnlam] = Field(default_factory=list)
    onemli_kolonlar: List[KolonAnlam] = Field(default_factory=list)
    kisaltmalar: Dict[str, str] = Field(default_factory=dict)
    aday_joinler: List[str] = Field(default_factory=list)
    tuzaklar: List[str] = Field(default_factory=list)
    kisisel_tablo_notu: Optional[str] = None

    @field_validator("anahtar_kolonlar", "aday_joinler", "tuzaklar", mode="before")
    @classmethod
    def _norm_lists(cls, v: Any) -> List[str]:
        return _to_str_list(v)

    @field_validator("tarih_kolonlari", "onemli_kolonlar", mode="before")
    @classmethod
    def _norm_kolonlar(cls, v: Any) -> List[Any]:
        if not isinstance(v, list):
            return []
        out = []
        for item in v:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                out.append({"kolon": item.strip(), "anlam": ""})
        return out

    @field_validator("kisaltmalar", mode="before")
    @classmethod
    def _norm_kisaltmalar(cls, v: Any) -> Dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k): str(val) for k, val in v.items() if k is not None and val is not None}


# =============================================================================
# 4. OpenAI TOOLS Üretici (`model_json_schema()`) ve Argüman Doğrulayıcı
# =============================================================================
TOOL_SPECS: List[Tuple[str, str, Type[BaseModel]]] = [
    (
        "bilgi_ara",
        "Bilgi tabanında arar: ilgili tablolar (kartları ve kullanım sayılarıyla), ekibin onayladığı "
        "sorgular, ekibin benzer geçmiş sorguları ve dönemsel tablo aileleri. İlk adım budur. Türkçe ve İngilizce "
        "eş anlamlıları ve kısaltmaları da ver (ör. gecikme, delay, dpd, dlq).",
        BilgiAraInput,
    ),
    (
        "tablo_karti",
        "Bir tablonun kartı: ne tuttuğu, satır seviyesi, kolonlar (tip, aralık, kod değerleri), "
        "doğrulanmış join'leri ve tuzakları. SQL yazmadan önce kullanacağın her tablo için çağır.",
        TabloKartiInput,
    ),
    (
        "join_yolu",
        "İki tablo arasındaki doğrulanmış join yolunu (ekibin sorgularından) verir. Join'i ASLA "
        "tahmin etme, bu aracı kullan.",
        JoinYoluInput,
    ),
    (
        "koken",
        "Tablonun hangi tablolardan üretildiğini ve hangi tabloları beslediğini verir.",
        KokenInput,
    ),
    (
        "son_donem",
        "Dönemsel tablo ailesinin (ör. PD_MAPPING, SME_WL) en yeni tablosunu ve dönemlerini verir.",
        SonDonemInput,
    ),
    (
        "sql_calistir",
        "Oracle'da SELECT sorgusu çalıştırır, satır sayısını ve ilk satırları döner. Ağır sorguda "
        "önce uyarı verir; kullanıcı onaylarsa zorla=true ile tekrar çağır.",
        SqlCalistirInput,
    ),
    (
        "tekillik_kontrol",
        "Join'li sorgunun satırları çoğaltıp çoğaltmadığını kontrol eder: COUNT(*) ile anahtar "
        "kolonların COUNT(DISTINCT) değerini karşılaştırır.",
        TekillikKontrolInput,
    ),
    (
        "plan_onayla",
        "Karmaşık sorularda (2+ tablo, çok adımlı hesap, dönem karşılaştırması) SQL yazmadan önce "
        "planı kullanıcıya onaylatır. Kullanıcının cevabını döner.",
        PlanOnaylaInput,
    ),
    (
        "kullaniciya_sor",
        "Kullanıcıya soru sorar ve cevabını döner: tablo seçimini onaylatmak, belirsizliği "
        "gidermek, ağır sorgu için izin almak.",
        KullaniciyaSorInput,
    ),
    (
        "tablo_olustur",
        "SADECE kullanıcı açıkça tablo oluşturmanı isterse: verilen SELECT'in sonucuyla "
        "kullanıcının KENDİ şemasında yeni bir tablo oluşturur (CREATE TABLE ... AS SELECT). Var olan tablonun "
        "üzerine yazmaz. Önce SELECT'i sql_calistir ile dene. Kullanıcı ekranda onaylayınca çalışır.",
        TabloOlusturInput,
    ),
    (
        "cevap_ver",
        "İş bitince çağrılır. Kısa Türkçe cevap, kullanılan nihai SQL (yoksa boş) ve varsayımlar.",
        CevapVerInput,
    ),
]

TOOL_SCHEMAS: Dict[str, Type[BaseModel]] = {name: model for name, _, model in TOOL_SPECS}


def _clean_json_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Pydantic v2 `model_json_schema()` çıktısını OpenAI function parameters biçimine sadeleştirir."""
    cleaned = dict(schema)
    cleaned.pop("title", None)
    props = {}
    for prop_name, prop_def in (cleaned.get("properties") or {}).items():
        if isinstance(prop_def, dict):
            p = dict(prop_def)
            p.pop("title", None)
            props[prop_name] = p
        else:
            props[prop_name] = prop_def
    cleaned["type"] = "object"
    cleaned["properties"] = props
    if "required" not in cleaned:
        cleaned["required"] = []
    return cleaned


def pydantic_to_openai_tool(name: str, description: str, model: Type[BaseModel]) -> Dict[str, Any]:
    """Bir Pydantic v2 modelini OpenAI `tools` elemanına çevirir."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": _clean_json_schema(model.model_json_schema()),
        },
    }


def build_openai_tools() -> List[Dict[str, Any]]:
    """Tüm ajan araçları için `model_json_schema()` tabanlı OpenAI TOOLS listesini üretir."""
    return [pydantic_to_openai_tool(name, desc, model) for name, desc, model in TOOL_SPECS]


def validate_tool_args(name: str, raw_args: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Araç çağrısı argümanlarını Pydantic v2 ile doğrular; `(temiz_dict, None)` veya `(None, hata)` döner."""
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return None, "Argümanlar okunamadı (çıktı yarıda kesilmiş). Bu aracı daha KISA argümanlarla tekrar çağır."
    elif isinstance(raw_args, dict):
        parsed = raw_args
    elif raw_args is None:
        parsed = {}
    else:
        return None, f"Geçersiz argüman tipi: {type(raw_args).__name__}"

    if not isinstance(parsed, dict):
        return None, "Araç argümanları JSON nesnesi (dict) olmalıdır."

    model = TOOL_SCHEMAS.get(name)
    if model is None:
        return parsed, None
    try:
        validated = model.model_validate(parsed)
        return validated.model_dump(), None
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', [])) or 'girdi'}: {err.get('msg', '')}"
            for err in exc.errors()[:4]
        )
        return None, f"Hatalı araç parametreleri ({name}): {issues}"
