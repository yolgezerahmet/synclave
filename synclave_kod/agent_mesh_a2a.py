#!/usr/bin/env python3
"""
agent_mesh_a2a.py — Minimal A2A (Agent2Agent) uyumlu mesh server (v0.1)

Linux Foundation A2A protokolünün JSON-RPC 2.0 alt kümesi:
  - GET  /.well-known/agent.json  → AgentCard (keşif)
  - POST /                        → JSON-RPC: task/send, task/get, task/cancel, message/send
  - Görevler "note" modunda yerel inbox'a yazılır (H3'ün Hermes'i işler)
    ve "status" modunda makine durumu döner.
  - Tailscale içi kullanım: sadece 100.x.x.x dinler (dışa kapalı), Bearer token.

Kurulum: python3 agent_mesh_a2a.py --port 8643 --token <TOKEN>
Client : a2a_cli.py send <host> <task> --token ...   /   a2a_cli.py get <host> <task_id>
"""
# pyright: reportOptionalMemberAccess=false
import argparse
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

# ─── Ajan kimliği (30 Ağu 2026) ────────────────────────────────────────
# Kopyalanamaz-kanıtlı kimlik + imzalı istek + sohbet etiketleme.
# Modül yoksa sunucu ESKİ davranışla (yalnız Bearer token) çalışmaya devam eder,
# böylece H2/H3 güncellenene kadar mesh kopmaz.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import agent_identity as AI
    IDENTITY_OK = True
except Exception as _e:  # pragma: no cover
    AI = None
    IDENTITY_OK = False
    _IDENT_ERR = str(_e)

_IDENT = None            # AgentIdentity (lazy)
REQUIRE_SIG = os.environ.get("A2A_REQUIRE_SIG", "").lower() in ("1", "true", "yes")


def identity():
    """Yerel ajan kimliği (tek kez yüklenir). Kimlik modülü yoksa None."""
    global _IDENT
    if _IDENT is None and IDENTITY_OK:
        try:
            _IDENT = AI.AgentIdentity.load_or_create()
        except Exception:
            return None
    return _IDENT

INBOX_DIR = Path(os.environ.get("A2A_INBOX", "~/.hermes/a2a_inbox")).expanduser()
TASK_STORE = Path(os.environ.get("A2A_TASK_STORE", "~/.hermes/a2a_tasks.json")).expanduser()

# ─── Kalıcı görev deposu (asenkron: server restart'ında kaybolmaz) ─────
def _load_tasks() -> dict:
    try:
        if TASK_STORE.exists():
            return json.loads(TASK_STORE.read_text())
    except Exception:
        pass
    return {}

def _save_tasks(tasks: dict):
    try:
        TASK_STORE.parent.mkdir(parents=True, exist_ok=True)
        TASK_STORE.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _push_notify(callback: str, task_id: str, status: str, result: dict):
    """Async görev tamamlanınca gönderen node'a İMZALI bildirim (gerçek zamanlı).

    Gönderenin a2a_cli'si metadata.callback = "IP:PORT" ekler; görev bitince
    buraya POST /notify atılır → gönderen polling YAPMADAN anında öğrenir.
    Güvenlik: kendi kimliğimizle imzalanır; alıcı TOFU + imza doğrular.
    """
    ident = identity()
    if not ident or not callback or ":" not in callback:
        return
    import urllib.request
    host, port = callback.rsplit(":", 1)
    try:
        port = int(port)
    except ValueError:
        return
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "task/notify",
                       "params": {"task_id": task_id, "status": status,
                                  "result": result}}).encode()
    headers = {"Content-Type": "application/json",
               **ident.sign_request("task/notify", body)}
    # Bearer token da gönder (check_auth her istekte zorunlu)
    tok = os.environ.get("A2A_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(
        f"http://{host}:{port}/", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
    except Exception:
        pass  # bildirim başarısız — gönderen yine de task/get ile alabilir

TASKS: dict = _load_tasks()


# ─── Platform yardımcıları (29 Ağu 2026 — H2/Windows portu) ───────────
# Orijinal kod 3 Linux'a özgü çağrı kullanıyordu, Windows'ta ÇÖKÜYORDU:
#   os.uname().nodename      → AttributeError (Windows'ta os.uname YOK)
#                              agent_card() ve status action ikisi de patlıyordu
#   os.stat("/proc/1")       → FileNotFoundError (/proc yok) → status 500
#   shutil.disk_usage("/")   → MSYS/Windows'ta kök disk belirsiz
# Linux/macOS davranışı AYNEN korunur; sadece fallback eklenir.
def _hostname() -> str:
    try:
        return os.uname().nodename          # Linux/macOS
    except AttributeError:
        return os.environ.get("COMPUTERNAME") or socket.gethostname()


def _root_path() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + os.sep
    return "/"


def _uptime_s():
    """Açılıştan beri geçen saniye. Windows: GetTickCount64 (bağımlılıksız)."""
    try:
        if os.name == "nt":
            import ctypes
            return round(ctypes.windll.kernel32.GetTickCount64() / 1000.0, 0)
        return round(time.time() - os.stat("/proc/1").st_mtime, 0)
    except Exception:
        return None

# ─── AgentCard (A2A keşif) ────────────────────────────────────────────
def agent_card(host: str, port: int) -> dict:
    card = {
        "protocolVersion": "1.0",
        "__version__": "1.1.0",
        "name": f"cumulus-agent-{_hostname()}",
        "description": "CumulusNET agent mesh — görev alır, yerel inbox'a yazar, durum döner",
        "url": f"http://{host}:{port}/",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "skills": [
            {"id": "note", "name": "Görev notu bırak", "description": "Görev metnini yerel inbox'a yazar (Hermes işler)"},
            {"id": "status", "name": "Makine durumu", "description": "Makine/disk/uptime özeti döner"},
        ],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
    }
    # Kriptografik kimlik: karşı taraf bu açık anahtarla imzamızı doğrular (TOFU)
    ident = identity()
    if ident:
        card["identity"] = ident.card()
        card["name"] = f"cumulus-{ident.runtime}-{ident.short}"
        card["capabilities"]["signedRequests"] = True
        card["capabilities"]["encryptedRequests"] = True
        # GERÇEK ZAMANLI (30 Ağu 2026): async görev bitince gönderen node'a
        # push bildirimi — karşı taraf polling yapmadan anında öğrenir.
        card["capabilities"]["pushNotifications"] = True
        card["capabilities"]["requireSignature"] = REQUIRE_SIG
    return card

# ─── Görev işleme ─────────────────────────────────────────────────────
def execute_task(task: dict):
    """Görevi işle → (status, result). 'note' inbox'a yazar, 'status' durum döner."""
    payload = task.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {"action": "note", "text": payload}
    action = payload.get("action", "note")
    if action == "status":
        import shutil
        disk = shutil.disk_usage(_root_path())
        out = {
            "host": _hostname(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "disk_gb": round(disk.free / 1e9, 1),
            "uptime_s": _uptime_s(),
        }
        ident = identity()
        if ident:
            out.update({"agent_id": ident.agent_id, "runtime": ident.runtime,
                        "clone_state": ident.meta.get("clone_state")})
        return "completed", out
    # note: inbox'a yaz (Hermes/otonom cron okur, işler, sonucu task sonucuna ekler)
    text = payload.get("text", json.dumps(payload, ensure_ascii=False))
    meta = task.get("metadata", {}) or {}
    peer = meta.get("from", "unknown")
    # Sohbet etiketi: hangi ajanla hangi kanalda konuştuğumuz kalıcı kaydedilir.
    # peer doğrulanmış agent_id ise "agent" sohbeti; değilse etiketsiz bırakılır
    # (uydurma kimlikle sohbet defteri kirletilmez).
    conv_id, msg_id = meta.get("conversation_id", ""), ""
    ident = identity()
    if ident and peer.startswith(("hx-", "oc-")) and meta.get("verified"):
        try:
            conv_id = conv_id or AI.open_conversation(
                "agent", peer, meta.get("channel", "a2a"), identity=ident)
            msg_id = AI.log_message(conv_id, "in", text, peer_id=peer,
                                    meta={"task_id": task.get("_task_id")})
        except Exception:
            pass
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(INBOX_DIR, 0o700)   # OCEANAPI DENETİMİ: dünya-okunabilir olmasın
    except Exception:
        pass
    fname = INBOX_DIR / f"{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
    fname.write_text(json.dumps({
        "ts": time.time(), "from": peer,
        "from_verified": bool(meta.get("verified")),
        "conversation_id": conv_id, "message_id": msg_id,
        "to_agent": ident.agent_id if ident else "",
        "text": text, "status": "new", "result": None,
        "_task_id": task.get("_task_id"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return "completed", {"note": str(fname), "inbox": str(INBOX_DIR),
                         "conversation_id": conv_id, "message_id": msg_id}

# ─── JSON-RPC dispatch (senkron / asenkron / sorgu) ───────────────────
def dispatch(method: str, params: dict) -> dict:
    if method == "task/send":
        task_id = uuid.uuid4().hex[:12]
        mode = params.get("mode", "sync")  # sync: anında sonuç | async: task_id + sonradan task/get
        params["_task_id"] = task_id
        TASKS[task_id] = {"status": "working", "result": None, "created": time.time(),
                          "mode": mode}
        _save_tasks(TASKS)
        if mode == "async":
            # arka planda işle (thread); sonuç store'a yazılır, task/get ile alınır
            import threading
            callback = (params.get("metadata") or {}).get("callback", "")
            def _run():
                try:
                    status, result = execute_task(params)
                    TASKS[task_id] = {"status": status, "result": result,
                                      "created": time.time(), "mode": "async"}
                    _push_notify(callback, task_id, status, result)
                except Exception as e:
                    TASKS[task_id] = {"status": "failed", "result": {"error": str(e)},
                                      "created": time.time(), "mode": "async"}
                    _push_notify(callback, task_id, "failed", {"error": str(e)})
                _save_tasks(TASKS)
            threading.Thread(target=_run, daemon=True).start()
            return {"id": task_id, "status": "working", "result": None, "mode": "async"}
        try:
            status, result = execute_task(params)
            TASKS[task_id] = {"status": status, "result": result, "created": time.time(),
                              "mode": "sync"}
        except Exception as e:
            TASKS[task_id] = {"status": "failed", "result": {"error": str(e)},
                              "created": time.time(), "mode": "sync"}
        _save_tasks(TASKS)
        return {"id": task_id, "status": TASKS[task_id]["status"], "result": TASKS[task_id]["result"]}
    if method == "task/get":
        tid = params.get("id", "")
        # ÖNCE inbox: worker işlenmişse güncel sonuç oradadır (H1 görev sonucu)
        for f in INBOX_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                if d.get("_task_id") == tid:
                    return {"id": tid, "status": d.get("status", "completed"),
                            "result": d.get("result") or {"note": str(f)}}
            except Exception:
                pass
        t = TASKS.get(tid)
        if not t:
            # FIX (31 Ağu): bilinmeyen task 400/KeyError yerine temiz 'not_found'
            # döner — ajanlar yanlış id sorgulayınca kafa karıştırıcı 400 almaz.
            return {"id": tid, "status": "not_found", "result": None}
        return {"id": tid, "status": t["status"], "result": t["result"], "mode": t.get("mode", "sync")}
    if method == "task/cancel":
        tid = params.get("id", "")
        if tid in TASKS:
            TASKS[tid]["status"] = "canceled"
            _save_tasks(TASKS)
        return {"id": tid, "status": "canceled"}
    if method == "message/send":
        return {"ok": True, "echo": params.get("message", ""), "mode": "sync"}
    if method == "message/sendSubscribe":
        # Canlı mesaj akışı (SSE) — dispatch içinde işlenmez; /stream endpoint'ine yönlendir
        return {"ok": True, "stream": f"/stream?message={params.get('message', '')[:50]}"}
    if method == "task/notify":
        # Diğer node'dan gelen PUSH bildirimi — async görev tamamlandı.
        # Alıcı taraf bu bildirimi "tamamlanan görev" olarak işaretler.
        tid = params.get("task_id", "")
        if tid:
            # bilinmeyen id de kaydedilir (gönderen taraf kendi takibini yapar)
            if tid not in TASKS:
                TASKS[tid] = {"status": "working", "result": None,
                              "created": time.time(), "mode": "async"}
            TASKS[tid]["status"] = params.get("status", "completed")
            TASKS[tid]["result"] = params.get("result")
            TASKS[tid]["notified_at"] = time.time()
            _save_tasks(TASKS)
        return {"ok": True, "notified": bool(tid)}
    if method == "task/list":
        # Uzak sunucudaki görev listesi (a2a_cli 'tasks' komutu).
        # TASKS: id -> {status, result, created, mode, ...}; en yeni önce.
        items = [
            {"id": tid, "status": t.get("status", "?"),
             "mode": t.get("mode", "sync"), "created": t.get("created")}
            for tid, t in TASKS.items()
        ]
        items.sort(key=lambda x: x.get("created") or 0, reverse=True)
        return {"tasks": items}
    raise KeyError(f"bilinmeyen metod: {method}")

# ─── FastAPI uygulaması ───────────────────────────────────────────────
def build_app(token: str):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Cumulus Agent Mesh (A2A)")

    def check_auth(request: Request):
        if not token:
            return True
        return request.headers.get("authorization") == f"Bearer {token}"

    @app.get("/.well-known/agent.json")
    async def card():
        return JSONResponse(agent_card("localhost", 8643))

    @app.post("/")
    async def rpc(request: Request):
        if not check_auth(request):
            return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32001, "message": "unauthorized"}}, status_code=401)
        raw = await request.body()
        req_id = 1  # parse'tan önce varsayılan (erken hata yolları için)

        # ── Kimlik katmanı ────────────────────────────────────────────
        # 1) Kendi kimliğimiz klon şüphesindeyse hiç görev almayız (fail-closed):
        #    kopyalanmış bir kurulum mesh'te iş yapamaz.
        ident = identity()
        if ident and ident.meta.get("clone_state") == "suspected":
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32003, "message": "local_identity_clone_suspected",
                "data": ident.meta.get("clone_detail", {})}}, status_code=403)

        # 2a) ŞİFRELİ GÖVDE (X-Agent-Enc: v1) — X25519 ECDH + AES-GCM + imza.
        #     Dinleme/saldırı koruması: gövde ağ üzerinde ŞİFRELİ taşınır.
        enc_flag = request.headers.get("x-agent-enc", "")
        if enc_flag and ident and IDENTITY_OK:
            try:
                outer = json.loads(raw)
                env = outer.get("enc")
                if not env:
                    raise ValueError("enc alanı yok")
                real_raw = AI.AgentIdentity.open_secure_payload(env, ident)
                # TOFU peer kaydı (şifreli paketteki kimlikle)
                peers = AI.load_peers(ident.runtime)
                peers[env["agent_id"]] = {
                    "public_key": env.get("public_key", ""),
                    "x25519_public": env.get("x25519_public", ""),
                    "runtime": env.get("runtime", "hermes"),
                    "label": env.get("machine_label", ""),
                    "first_seen": peers.get(env.get("agent_id"), {}).get(
                        "first_seen") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "msg_count": int(peers.get(env.get("agent_id"), {}).get(
                        "msg_count", 0)) + 1,
                }
                AI.save_peers(peers, ident.runtime)
                raw = real_raw
                vr = {"ok": True, "agent_id": env["agent_id"],
                      "reason": "verified_encrypted"}
            except Exception as e:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32002, "message": f"identity_rejected:encrypted:{e}",
                    "data": {}}}, status_code=403)
        else:
            vr = {"ok": True, "agent_id": "anonymous", "reason": "identity_module_off"}

        try:
            body = json.loads(raw)
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}, status_code=400)
        method = body.get("method")
        params = body.get("params", {})
        req_id = body.get("id", 1)

        # 2b) Şifresiz gövde → imza doğrula (Ed25519 + TOFU + replay koruması).
        if IDENTITY_OK and vr["reason"] != "verified_encrypted":
            vr = AI.verify_request(dict(request.headers), method or "", raw,
                                   require=REQUIRE_SIG)
            if not vr["ok"]:
                return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32002, "message": f"identity_rejected:{vr['reason']}",
                    "data": {"agent_id": vr.get("agent_id")}}}, status_code=403)
        # 3) Doğrulanmış kimliği göreve işle → sohbet etiketi buradan üretilir
        if isinstance(params, dict):
            md = dict(params.get("metadata", {}) or {})
            md["from"] = vr["agent_id"] if vr["reason"].startswith("verified") else md.get("from", "unknown")
            md["verified"] = vr["reason"].startswith("verified")
            md.setdefault("channel", "a2a")
            if request.headers.get("x-conversation-id"):
                md["conversation_id"] = request.headers["x-conversation-id"]
            params["metadata"] = md

        try:
            result = dispatch(method, params)
            if isinstance(result, dict) and ident:
                result["served_by"] = ident.agent_id
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        except KeyError as e:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}}, status_code=400)
        except Exception as e:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}}, status_code=500)

    @app.get("/health")
    async def health():
        ident = identity()
        out = {"status": "ok", "tasks": len(TASKS), "host": _hostname()}
        if ident:
            out.update({"agent_id": ident.agent_id, "runtime": ident.runtime,
                        "clone_state": ident.meta.get("clone_state"),
                        "require_signature": REQUIRE_SIG})
            if ident.meta.get("clone_state") == "suspected":
                out["status"] = "degraded"
        return out

    @app.get("/identity")
    async def identity_endpoint(request: Request):
        """Açık kimlik + tanınan ajanlar + sohbet sayacı (özel anahtar YOK).
        OCEANAPI DENETİMİ: endpoint yetkilendirme gerektirir — konuşma
        metadata'sı dışarı sızmasın (401 reddi)."""
        if not check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        ident = identity()
        if not ident:
            return JSONResponse({"error": "identity_module_off"}, status_code=503)
        convs = AI.list_conversations(ident.runtime, limit=200)
        return {"identity": ident.card(),
                "peers": {k: {kk: vv for kk, vv in v.items() if kk != "public_key"}
                          for k, v in AI.load_peers(ident.runtime).items()},
                "conversations": {"total": len(convs),
                                  "user": sum(1 for c in convs if c["kind"] == "user"),
                                  "agent": sum(1 for c in convs if c["kind"] == "agent"),
                                  "recent": convs[:10]}}

    @app.get("/stream")
    async def stream(message: str = "", seconds: int = 10):
        """Canlı (SSE) mesaj akışı — 2 saniyede bir durum/mesaj yayınlar.
        Kullanım: GET /stream?message=selam&seconds=10
        (canlı görüşme kanalı; ajan bu akışı dinleyerek eşzamanlı konuşur)
        OCEANAPI DENETİMİ: message/seconds sınırlandı (kaynak tüketimi engeli).
        """
        from fastapi.responses import StreamingResponse

        seconds = min(max(int(seconds), 1), 60)   # 1..60s üst sınır
        message = message[:200]                    # 200 char üst sınır

        async def gen():
            import asyncio as _asyncio
            start = time.time()
            yield "event: open\ndata: {\"status\": \"connected\"}\n\n"
            i = 0
            while time.time() - start < seconds:
                yield (f"event: message\ndata: {json.dumps({'t': i, 'echo': message, 'ts': time.strftime('%H:%M:%S')}, ensure_ascii=False)}\n\n")
                i += 1
                await _asyncio.sleep(2)
            yield "event: done\ndata: {}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/events")
    async def events(request: Request, seconds: int = 30):
        """GERÇEK ZAMANLI görev akışı (SSE) — async görev durum değişimlerini
        canlı yayınlar. Kullanım: GET /events?seconds=30
        (node'lar birbirinin görevlerini anında görür; polling GEREKMEZ)."""
        from fastapi.responses import StreamingResponse
        if not check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        seconds = min(max(int(seconds), 5), 300)
        last_len = len(TASKS)
        nonlocal_guard = {"last_len": last_len}

        async def gen():
            import asyncio as _asyncio
            start = time.time()
            yield "event: open\ndata: {\"status\": \"connected\", \"tasks\": " + str(nonlocal_guard["last_len"]) + "}\n\n"
            while time.time() - start < seconds:
                cur = len(TASKS)
                done = [{"id": k, "status": v.get("status"),
                         "ts": v.get("created")}
                        for k, v in list(TASKS.items())[-3:]]
                if cur != nonlocal_guard["last_len"] or done:
                    yield f"event: task\ndata: {json.dumps({'total': cur, 'recent': done}, ensure_ascii=False)}\n\n"
                    nonlocal_guard["last_len"] = cur
                await _asyncio.sleep(1)
            yield "event: done\ndata: {}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8643)
    ap.add_argument("--host", default="0.0.0.0")  # Tailscale arayüzünde dinle; dış UFW kapalı
    ap.add_argument("--token", default=os.environ.get("A2A_TOKEN", ""))
    ap.add_argument("--require-signature", action="store_true",
                    help="imzasız istekleri REDDET (tüm node'lar güncellendikten sonra aç)")
    args = ap.parse_args()
    global REQUIRE_SIG
    if args.require_signature:
        REQUIRE_SIG = True
    # OCEANAPI DENETİMİ: boş token fail-closed uyarısı — doğrulama imza
    # katmanına düşer; production'da token zorunlu önerilir.
    if not args.token:
        print("UYARI: A2A_TOKEN boş — doğrulama yalnız imza/rate-limit katmanına "
              "dayanır. Production'da --token veya A2A_TOKEN env zorunlu.",
              file=sys.stderr)
    try:
        import uvicorn
    except ImportError:
        # Windows (H2) dahil temiz kurulum hatası — ham traceback yerine
        print("HATA: 'uvicorn' paketi yok — A2A mesh server başlatılamaz.\n"
              "      Kur: pip install uvicorn  (veya pip install 'synclave[a2a]')")
        sys.exit(1)
    app = build_app(args.token)
    ident = identity()
    kimlik = (f"{ident.agent_id} [{ident.runtime}] klon={ident.meta.get('clone_state')}"
              if ident else f"KİMLİK YOK ({_IDENT_ERR if not IDENTITY_OK else 'yüklenemedi'})")
    print(f"A2A mesh server: http://{args.host}:{args.port} "
          f"(token={'var' if args.token else 'YOK'}, imza_zorunlu={REQUIRE_SIG})")
    print(f"  kimlik: {kimlik}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

if __name__ == "__main__":
    main()
