# -*- coding: utf-8 -*-
"""
agent_graph.py - dwh_agent için LangGraph StateGraph + interrupt() Human-in-the-Loop motoru.

Mevcut `ajan_core.Session` / `run` / `resume` / `plan_onayla` / `kullaniciya_sor` / `tablo_olustur`
akışını LangGraph `StateGraph` + `interrupt()` + `Command(resume=...)` + checkpointer
(`MemorySaver` veya yerel `SqliteSaver`) olarak kurgular.

Özellikler:
  - Streamlit (`ajan_app.py`) oturum modeliyle birebir uyumlu (`GraphSession`, `run_graph`, `resume_graph`)
  - Saf JSON-serileştirilebilir grafik durumu (`AgentGraphState`) -> Docker/Postgres gerekmez
  - `get_mermaid_diagram()` ile canlı Mermaid akış diyagramı çıktısı
  - Python 3.9+ uyumlu (`from __future__ import annotations`)
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import ajan_core as core
import schemas

APPROVAL_TOOLS = set(core.PAUSE_TOOLS) | {"tablo_olustur"}


class AgentGraphState(TypedDict, total=False):
    """LangGraph düğümleri arasında taşınan serileştirilebilir ajan durumu."""

    question: str
    messages: List[Dict[str, Any]]
    steps: List[Dict[str, str]]
    pending_calls: List[Dict[str, Any]]
    n_llm: int
    result: Optional[Dict[str, str]]
    status: str  # "new" | "running" | "waiting" | "done" | "error"
    error: Optional[str]
    auto_approve: bool


def create_checkpointer(sqlite_path: Optional[str] = None) -> Any:
    """Yerel checkpointer döner: yol verilirse SqliteSaver, verilmezse MemorySaver."""
    if not sqlite_path:
        return MemorySaver()
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        return SqliteSaver(conn)
    except Exception:
        return MemorySaver()


def _normalize_tool_calls(calls: List[Dict[str, Any]], step_no: int) -> List[Dict[str, Any]]:
    norm: List[Dict[str, Any]] = []
    for i, c in enumerate(calls or []):
        fn = c.get("function") or {}
        raw = fn.get("arguments") or "{}"
        norm.append(
            {
                "id": c.get("id") or f"c{step_no}_{i}",
                "type": "function",
                "function": {
                    "name": fn.get("name"),
                    "arguments": raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False),
                },
            }
        )
    return norm


def build_agent_graph(
    ctx: Optional[core.Ctx] = None,
    checkpointer: Optional[Any] = None,
    on_step: Optional[Callable[[Dict[str, str]], None]] = None,
    runtime: Optional[Dict[str, Any]] = None,
) -> Any:
    """DWH ajanının LangGraph StateGraph akışını derler ve döner.

    Düğümler:
      - llm_node: IBThink üzerinden sonraki adımı / araç çağrılarını alır
      - tool_node: Okuma/analiz araçlarını ve cevap_ver/auto_approve adımlarını çalıştırır
      - human_approval_node: plan_onayla, kullaniciya_sor ve tablo_olustur için interrupt() ile durur
      - finalize_node: Nihai sonucu ve oturum durumunu sonlandırır
    """
    rt = runtime if runtime is not None else {"ctx": ctx, "on_step": on_step}

    def _get_ctx() -> core.Ctx:
        c = rt.get("ctx")
        if c is None:
            raise RuntimeError("LangGraph çalışma bağlamı (ctx) tanımlı değil.")
        return c

    def _notify(step: Dict[str, str]) -> None:
        cb = rt.get("on_step")
        if cb is not None:
            cb(step)

    def llm_node(state: AgentGraphState) -> Dict[str, Any]:
        messages = list(state.get("messages") or [])
        n_llm = int(state.get("n_llm") or 0)
        if n_llm >= core.MAX_STEPS:
            c = _get_ctx()
            return {
                "result": {
                    "cevap": f"{core.MAX_STEPS} adım sınırına ulaşıldı; soru çözülemedi.",
                    "sql": c.last_sql or "",
                    "varsayimlar": "",
                },
                "status": "done",
                "pending_calls": [],
            }
        try:
            msg = core.llm_chat(messages, core.TOOLS)
            n_llm += 1
            if msg.get("_finish") == "length":
                msg = core.llm_chat(messages, core.TOOLS, no_think=True)
                n_llm += 1
        except Exception as exc:
            return {
                "n_llm": n_llm,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "pending_calls": [],
            }

        content = core.strip_think(msg.get("content"))
        norm = _normalize_tool_calls(msg.get("tool_calls") or [], n_llm)
        if not norm:
            c = _get_ctx()
            return {
                "n_llm": n_llm,
                "result": {"cevap": content, "sql": c.last_sql or "", "varsayimlar": ""},
                "status": "done",
                "pending_calls": [],
            }
        messages.append({"role": "assistant", "content": content, "tool_calls": norm})
        return {
            "messages": messages,
            "n_llm": n_llm,
            "pending_calls": norm,
            "status": "running",
        }

    def tool_node(state: AgentGraphState) -> Dict[str, Any]:
        c = _get_ctx()
        messages = list(state.get("messages") or [])
        steps = list(state.get("steps") or [])
        calls = list(state.get("pending_calls") or [])
        auto_approve = bool(state.get("auto_approve", False))

        idx = 0
        while idx < len(calls):
            call = calls[idx]
            name = call["function"]["name"]

            # İnsan onayı gerektiren araç gelince dur ve human_approval_node'a devret
            if not auto_approve and name in APPROVAL_TOOLS:
                return {
                    "messages": messages,
                    "steps": steps,
                    "pending_calls": calls[idx:],
                    "status": "running",
                }

            args, err_msg = schemas.validate_tool_args(name, call["function"].get("arguments"))
            if err_msg is not None:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": err_msg})
                step = {"arac": name, "girdi": core.short(call["function"].get("arguments"), 300), "cikti": err_msg}
                steps.append(step)
                _notify(step)
                idx += 1
                continue

            if name == "cevap_ver":
                if len(str(args.get("cevap") or "").strip()) < 15:
                    msg = (
                        "cevap alanı boş ya da yarım geldi. cevap_ver'i tekrar çağır: 'cevap' alanına "
                        "3-10 satırlık tam bir Türkçe cevap yaz (sonuç tablosunu cevaba kopyalama, ekranda zaten görünüyor)."
                    )
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": msg})
                    step = {"arac": name, "girdi": core.short(args.get("cevap"), 300), "cikti": msg}
                    steps.append(step)
                    _notify(step)
                    idx += 1
                    continue
                result = {
                    "cevap": args.get("cevap", ""),
                    "sql": args.get("sql") or c.last_sql or "",
                    "varsayimlar": args.get("varsayimlar", ""),
                }
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": "Cevap kaydedildi."})
                step = {"arac": name, "girdi": "", "cikti": str(args.get("cevap") or "")}
                steps.append(step)
                _notify(step)
                return {
                    "messages": messages,
                    "steps": steps,
                    "pending_calls": [],
                    "result": result,
                    "status": "done",
                }

            if name == "tablo_olustur":
                # auto_approve=True (ölçüm / otomatik mod) -> tablo oluşturulmaz
                prep, err = c.tablo_hazirla(
                    **{k: v for k, v in args.items() if k in ("tablo_adi", "sql", "aciklama", "zorla")}
                )
                out = err or "Ölçüm/otomatik modda tablo oluşturulmaz."
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
                step = {"arac": name, "girdi": json.dumps(args, ensure_ascii=False), "cikti": out}
                steps.append(step)
                _notify(step)
                idx += 1
                continue

            if name in core.PAUSE_TOOLS:
                # auto_approve=True
                text = args.get("plan") or args.get("soru") or ""
                answer = "Onaylandı, devam et."
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": answer})
                step = {"arac": name, "girdi": text, "cikti": answer}
                steps.append(step)
                _notify(step)
                idx += 1
                continue

            out = core._exec_tool(c, name, args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": core.short(out, core.TOOL_OUTPUT_CHARS)})
            step = {"arac": name, "girdi": json.dumps(args, ensure_ascii=False), "cikti": core.short(out, 1500)}
            steps.append(step)
            _notify(step)
            idx += 1

        return {
            "messages": messages,
            "steps": steps,
            "pending_calls": [],
            "status": "running",
        }

    def human_approval_node(state: AgentGraphState) -> Dict[str, Any]:
        """Human-in-the-Loop düğümü: `interrupt()` ile durur, kullanıcı yanıtıyla devam eder."""
        c = _get_ctx()
        messages = list(state.get("messages") or [])
        steps = list(state.get("steps") or [])
        calls = list(state.get("pending_calls") or [])
        if not calls:
            return {"status": "running"}

        call = calls[0]
        rest = calls[1:]
        name = call["function"]["name"]

        args, err_msg = schemas.validate_tool_args(name, call["function"].get("arguments"))
        if err_msg is not None:
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": err_msg})
            step = {"arac": name, "girdi": core.short(call["function"].get("arguments"), 300), "cikti": err_msg}
            steps.append(step)
            _notify(step)
            return {"messages": messages, "steps": steps, "pending_calls": rest, "status": "running"}

        if name == "tablo_olustur":
            prep, err = c.tablo_hazirla(
                **{k: v for k, v in args.items() if k in ("tablo_adi", "sql", "aciklama", "zorla")}
            )
            if err:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": err})
                step = {"arac": name, "girdi": json.dumps(args, ensure_ascii=False), "cikti": err}
                steps.append(step)
                _notify(step)
                return {"messages": messages, "steps": steps, "pending_calls": rest, "status": "running"}

            tahmin = f" (tahmini {int(prep['satir_tahmini']):,} satır)" if prep.get("satir_tahmini") else ""
            text = (
                f"Ajan şemanda yeni bir tablo oluşturmak istiyor: **{prep['owner']}.{prep['ad']}**{tahmin}\n\n"
                f"{prep['aciklama']}\n\n```sql\nCREATE TABLE {prep['owner']}.{prep['ad']} AS\n{prep['sql']}\n```\n"
                f"Onaylarsan oluşturulur. İstemiyorsan düzeltmeni yazıp 'Düzeltmeyi gönder'e bas."
            )
            payload = {
                "call_id": call["id"],
                "kind": name,
                "text": text,
                "girdi": json.dumps(args, ensure_ascii=False),
                "prep": prep,
            }
            answer = interrupt(payload)
            content = (
                c.tablo_olustur_calistir(prep)
                if str(answer).startswith("Onaylandı")
                else f"Kullanıcı tablo oluşturmayı onaylamadı. Kullanıcının cevabı: {answer}"
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
            step = {"arac": name, "girdi": payload["girdi"], "cikti": f"Kullanıcı: {answer}\n{content}"}
            steps.append(step)
            _notify(step)
            return {"messages": messages, "steps": steps, "pending_calls": rest, "status": "running"}

        # plan_onayla veya kullaniciya_sor
        text = args.get("plan") or args.get("soru") or ""
        payload = {"call_id": call["id"], "kind": name, "text": text, "girdi": text}
        answer = interrupt(payload)
        content = f"Kullanıcının cevabı: {answer}"
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        step = {"arac": name, "girdi": text, "cikti": f"Kullanıcı: {answer}"}
        steps.append(step)
        _notify(step)
        return {"messages": messages, "steps": steps, "pending_calls": rest, "status": "running"}

    def finalize_node(state: AgentGraphState) -> Dict[str, Any]:
        status = state.get("status") or "done"
        if status == "error":
            return {"status": "error"}
        result = state.get("result")
        if result is None:
            c = _get_ctx()
            result = {
                "cevap": f"{core.MAX_STEPS} adım sınırına ulaşıldı; soru çözülemedi.",
                "sql": c.last_sql or "",
                "varsayimlar": "",
            }
        return {"result": result, "status": "done"}

    def route_after_llm(state: AgentGraphState) -> str:
        if state.get("status") in ("done", "error"):
            return "finalize"
        calls = state.get("pending_calls") or []
        if not calls:
            return "finalize"
        next_name = calls[0]["function"]["name"]
        if not state.get("auto_approve", False) and next_name in APPROVAL_TOOLS:
            return "human_approval"
        return "tools"

    def route_after_tools(state: AgentGraphState) -> str:
        if state.get("status") in ("done", "error"):
            return "finalize"
        calls = state.get("pending_calls") or []
        if calls:
            next_name = calls[0]["function"]["name"]
            if not state.get("auto_approve", False) and next_name in APPROVAL_TOOLS:
                return "human_approval"
            return "tools"
        if int(state.get("n_llm") or 0) >= core.MAX_STEPS:
            return "finalize"
        return "llm"

    builder = StateGraph(AgentGraphState)
    builder.add_node("llm", llm_node)
    builder.add_node("tools", tool_node)
    builder.add_node("human_approval", human_approval_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "llm")
    builder.add_conditional_edges(
        "llm",
        route_after_llm,
        {"tools": "tools", "human_approval": "human_approval", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "tools",
        route_after_tools,
        {"tools": "tools", "human_approval": "human_approval", "llm": "llm", "finalize": "finalize"},
    )
    builder.add_conditional_edges(
        "human_approval",
        route_after_tools,
        {"tools": "tools", "human_approval": "human_approval", "llm": "llm", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)

    cp = checkpointer if checkpointer is not None else MemorySaver()
    return builder.compile(checkpointer=cp)


def get_mermaid_diagram(graph: Optional[Any] = None) -> str:
    """Derlenmiş LangGraph StateGraph akışının Mermaid diyagram metnini döndürür."""
    g = graph if graph is not None else build_agent_graph()
    return g.get_graph().draw_mermaid()


# =============================================================================
# Streamlit / ajan_core.Session uyumlu oturum köprüsü
# =============================================================================
class GraphSession(core.Session):
    """`ajan_core.Session` ile birebir uyumlu, LangGraph thread ve checkpointer destekli oturum."""

    def __init__(
        self,
        question: str,
        extra_system: str = "",
        thread_id: Optional[str] = None,
        checkpointer: Optional[Any] = None,
    ):
        super().__init__(question, extra_system=extra_system)
        self.thread_id: str = thread_id or f"dwh-{uuid.uuid4().hex[:12]}"
        self._checkpointer: Any = checkpointer if checkpointer is not None else MemorySaver()
        self._runtime: Dict[str, Any] = {"ctx": None, "on_step": None}
        self._graph: Any = build_agent_graph(checkpointer=self._checkpointer, runtime=self._runtime)

    def mermaid(self) -> str:
        """Bu oturumun LangGraph akış diyagramını Mermaid formatında döner."""
        return get_mermaid_diagram(self._graph)


def _ensure_graph_session(sess: core.Session, checkpointer: Optional[Any] = None) -> None:
    """Düz `core.Session` verilse bile LangGraph checkpointer ve thread_id ekler."""
    if not hasattr(sess, "thread_id"):
        sess.thread_id = f"dwh-{uuid.uuid4().hex[:12]}"
    if not hasattr(sess, "_runtime"):
        sess._runtime = {"ctx": None, "on_step": None}
    if not hasattr(sess, "_graph"):
        cp = checkpointer if checkpointer is not None else MemorySaver()
        sess._checkpointer = cp
        sess._graph = build_agent_graph(checkpointer=cp, runtime=sess._runtime)


def _sync_from_output(
    sess: core.Session,
    ctx: core.Ctx,
    out: Dict[str, Any],
    on_step: Optional[Callable[[Dict[str, str]], None]],
) -> str:
    """LangGraph çıktı/kesinti durumunu `Session` alanlarına (Streamlit uyumlu) aktarır."""
    sess.messages = list(out.get("messages") or sess.messages)
    sess.steps = list(out.get("steps") or sess.steps)
    sess.n_llm = int(out.get("n_llm") or sess.n_llm)
    sess.result = out.get("result")
    sess.error = out.get("error")

    interrupts = out.get("__interrupt__") or []
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", first)
        if not isinstance(payload, dict):
            payload = {"call_id": "", "kind": "kullaniciya_sor", "text": str(payload)}
        sess.pending = payload
        sess.status = "waiting"
        wait_label = "(kullanıcı onayı bekleniyor)" if payload.get("kind") == "tablo_olustur" else "(kullanıcı cevabı bekleniyor)"
        wait_step = {
            "arac": payload.get("kind", "kullaniciya_sor"),
            "girdi": payload.get("girdi", payload.get("text", "")),
            "cikti": wait_label,
        }
        sess.steps.append(wait_step)
        if on_step is not None:
            on_step(wait_step)
        return "waiting"

    sess.pending = None
    sess.status = out.get("status") or "done"
    if sess.status == "error":
        core.log(sess, ctx)
        return "error"
    return core.finish(sess, ctx)


def run_graph(
    sess: core.Session,
    ctx: core.Ctx,
    on_step: Optional[Callable[[Dict[str, str]], None]] = None,
    auto_approve: bool = False,
    checkpointer: Optional[Any] = None,
) -> str:
    """LangGraph StateGraph üzerinde oturumu başlatır; 'done' | 'waiting' | 'error' döner."""
    _ensure_graph_session(sess, checkpointer=checkpointer)
    sess._runtime["ctx"] = ctx
    sess._runtime["on_step"] = on_step
    sess.status = "running"

    initial_state: AgentGraphState = {
        "question": sess.question,
        "messages": list(sess.messages),
        "steps": list(sess.steps),
        "pending_calls": [],
        "n_llm": sess.n_llm,
        "result": sess.result,
        "status": "running",
        "error": None,
        "auto_approve": bool(auto_approve),
    }
    config = {"configurable": {"thread_id": sess.thread_id}, "recursion_limit": max(100, core.MAX_STEPS * 4)}
    try:
        out = sess._graph.invoke(initial_state, config=config)
        return _sync_from_output(sess, ctx, out, on_step)
    except Exception as exc:
        sess.status, sess.error = "error", f"{type(exc).__name__}: {exc}"
        core.log(sess, ctx)
        return "error"


def resume_graph(
    sess: core.Session,
    ctx: core.Ctx,
    answer: str,
    on_step: Optional[Callable[[Dict[str, str]], None]] = None,
    auto_approve: bool = False,
) -> str:
    """`interrupt()` ile bekleyen LangGraph oturumunu `Command(resume=answer)` ile devam ettirir."""
    _ensure_graph_session(sess)
    sess._runtime["ctx"] = ctx
    sess._runtime["on_step"] = on_step
    sess.status = "running"
    sess.pending = None

    config = {"configurable": {"thread_id": sess.thread_id}, "recursion_limit": max(100, core.MAX_STEPS * 4)}
    try:
        out = sess._graph.invoke(Command(resume=answer), config=config)
        return _sync_from_output(sess, ctx, out, on_step)
    except Exception as exc:
        sess.status, sess.error = "error", f"{type(exc).__name__}: {exc}"
        core.log(sess, ctx)
        return "error"
