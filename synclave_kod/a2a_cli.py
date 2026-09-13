#!/usr/bin/env python3
"""
a2a_cli.py — A2A mesh client (CumulusNET)

Kullanım:
  a2a_cli.py send <host> "<görev metni>" [--token X]     → task_id + durum
  a2a_cli.py send-status <host> [--token X]              → makine durumu iste
  a2a_cli.py get <host> <task_id> [--token X]            → sonuç
  a2a_cli.py card <host> [--token X]                     → AgentCard
  a2a_cli.py ping <host> [--token X]                     → sağlık

Host örnekleri: 100.103.44.107 (H3), 100.92.2.47 (H1), 100.76.82.46 (H2)
"""
# pyright: reportOptionalMemberAccess=false
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_CARD_CACHE: dict[str, tuple] = {}
_CARD_TTL = 300
try:
    import agent_identity as AI
    IDENTITY_OK = True
except Exception:
    AI = None
    IDENTITY_OK = False

_IDENT = None


def identity():
    """Yerel kimlik (imza için). Yoksa imzasız gönderilir (eski davranış)."""
    global _IDENT
    if _IDENT is None and IDENTITY_OK:
        try:
            _IDENT = AI.AgentIdentity.load_or_create()
        except Exception:
            return None
    return _IDENT


def peer_card(host: str, port: int = 8643):
    """Karşı tarafın AgentCard'ı (önbellekli). Şifreli gönderim için X25519
    açık anahtarı ve şifreleme desteği buradan alınır."""
    now = time.time()
    key = f"{host}:{port}"
    hit = _CARD_CACHE.get(key)
    if hit and now - hit[1] < _CARD_TTL:
        return hit[0]
    req = urllib.request.Request(f"http://{host}:{port}/.well-known/agent.json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            card = json.loads(resp.read().decode())
        _CARD_CACHE[key] = (card, now)
        return card
    except Exception:
        return hit[0] if hit else {}


def canonical_host(host: str) -> str:
    """Kanonik H1/H2/H3 takma adlarını tek IP'ye çevir."""
    aliases = {
        "h1": "100.92.2.47",
        "cumulusnet-hermes-1": "100.92.2.47",
        "h2": "100.76.82.46",
        "sistemg16": "100.76.82.46",
        "h3": "100.103.44.107",
        "hermesagent03": "100.103.44.107",
    }
    return aliases.get(host.strip().lower(), host.strip())


def rpc(host: str, method: str, params: dict, token: str, port: int = 8643,
        sign: bool = True, conv_id: str = "", encrypt: bool = True,
        retries: int = 0):
    # Yeniden gönderim yalnızca health gibi salt-okunur RPC'lerde güvenlidir.
    # İmzalı/şifreli task isteğinde aynı nonce ile retry çift görev veya replay
    # reddi üretebilir; bu yol açıkça kapalı tutulur.
    if retries and method != "ping":
        retries = 0
    host = canonical_host(host)
    url = f"http://{host}:{port}/"
    # GERÇEK ZAMANLI (30 Ağu 2026): async görev gönderirken kendi callback
    # adresimizi ekle → karşı taraf görev bitince BİZE push eder (polling yok).
    if method == "task/send" and isinstance(params, dict):
        md = dict(params.get("metadata", {}) or {})
        md.setdefault("callback", f"{_self_addr()}:8643")
        params["metadata"] = md
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ident = identity() if sign else None
    if ident:
        # Klon şüphesinde kendi kimliğimizle mesaj GÖNDERMEYİZ (fail-closed)
        ident.assert_not_clone()
        card = peer_card(host, port)
        peer_enc = (card.get("capabilities") or {}).get("encryptedRequests")
        peer_x = (card.get("identity") or {}).get("x25519_public", "")
        if encrypt and peer_enc and peer_x:
            # ŞİFRELİ gövde: X25519 ECDH + AES-GCM (PFS) + Ed25519 imza
            peer_aid = (card.get("identity") or {}).get("agent_id", "")
            env = ident.secure_payload(peer_x, body, to_agent=peer_aid)
            env["runtime"] = ident.runtime
            env["machine_label"] = ident.meta.get("machine_label", "")
            body2 = json.dumps({"enc": env}).encode()
            req.data = body2
            req.add_header("X-Agent-Enc", "v1")
            req.add_header("X-Agent-Label", ident.meta.get("machine_label", ""))
            if conv_id:
                req.add_header("X-Conversation-Id", conv_id)
        else:
            # İmzalı ama düz gövde (eski sunucu / şifreleme yok)
            for k, v in ident.sign_request(method, body).items():
                req.add_header(k, v)
            req.add_header("X-Agent-Label", ident.meta.get("machine_label", ""))
            if conv_id:
                req.add_header("X-Conversation-Id", conv_id)
    attempts = max(0, int(retries)) + 1
    out = None
    last_error = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode())
            break
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(2 ** attempt, 4))
    if out is None:
        raise RuntimeError(f"A2A yanıtı alınamadı: {last_error}")
    # Giden mesajı yerel sohbet defterine işle (karşı tarafın agent_id'si ile)
    peer = ((out.get("result") or {}).get("served_by") or "") if isinstance(out, dict) else ""
    if ident and peer.startswith(("hx-", "oc-")):
        try:
            cid = conv_id or AI.open_conversation("agent", peer, "a2a", identity=ident)
            mid = AI.log_message(cid, "out", body, peer_id=peer,
                                 meta={"method": method, "host": host})
            if isinstance(out, dict):
                out["_local_conversation_id"] = cid
                out["_local_message_id"] = mid
        except Exception:
            pass
    return out


def _self_addr() -> str:
    """Kendi (gönderen) Tailscale/IP adresini bul — push bildiriminin hedefi.
    Öncelik: A2A_CALLBACK env > Tailscale IP (tailscale ip -4) > 100.x ağı."""
    env = os.environ.get("A2A_CALLBACK", "")
    if env and ":" in env:
        return env.split(":")[0]
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            ip = r.stdout.strip().split()[0]
            if ip.startswith("100."):
                return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("100.64.0.1", 9))  # Tailscale ağına bağlan (dışarı paket yok)
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("komut", choices=["send", "send-status", "get", "card", "ping", "stream",
                                      "update", "info", "tasks"])
    ap.add_argument("host")
    ap.add_argument("gorev", nargs="?", default="")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--port", type=int, default=8643)
    ap.add_argument("--mode", choices=["sync", "async"], default="sync")
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--no-sign", action="store_true", help="imzasız gönder (eski uyum)")
    ap.add_argument("--conv", default="", help="mevcut sohbet ID'si ile devam et")
    args = ap.parse_args()
    args.host = canonical_host(args.host)
    sign = not args.no_sign

    try:
        if args.komut == "send":
            r = rpc(args.host, "task/send", {"payload": {"action": "note", "text": args.gorev},
                                             "mode": args.mode}, args.token, args.port,
                    sign=sign, conv_id=args.conv)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.komut == "send-status":
            r = rpc(args.host, "task/send", {"payload": {"action": "status"}, "mode": args.mode},
                    args.token, args.port, sign=sign, conv_id=args.conv)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.komut == "get":
            r = rpc(args.host, "task/get", {"id": args.task_id}, args.token, args.port,
                    sign=sign, conv_id=args.conv)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.komut == "card":
            req = urllib.request.Request(f"http://{args.host}:{args.port}/.well-known/agent.json")
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(resp.read().decode())
        elif args.komut == "ping":
            req = urllib.request.Request(f"http://{args.host}:{args.port}/health")
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(resp.read().decode())
        elif args.komut == "update":
            # Kullanım: update <host> URL#SHA256 — güvenli agent-update görevi gönderir.
            if not args.gorev or "#" not in args.gorev:
                raise SystemExit("update için URL#SHA256 gerekli: update h2 http://...tar.gz#<sha>")
            url, sha = args.gorev.rsplit("#", 1)
            try:
                from synclave.inbox_worker import build_agent_update_task
            except ImportError:
                from inbox_worker import build_agent_update_task
            task = build_agent_update_task(url, sha)
            r = rpc(args.host, "task/send",
                    {"payload": {"action": "note", "text": task}, "mode": args.mode},
                    args.token, args.port, sign=sign, conv_id=args.conv)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.komut == "info":
            # Kimlik + sağlık tek çıktıda (çift taraflı düğüm görünürlüğü).
            card = peer_card(args.host, args.port)
            health = {}
            try:
                req = urllib.request.Request(f"http://{args.host}:{args.port}/health")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    health = json.loads(resp.read().decode())
            except Exception as e:
                health = {"error": str(e)}
            print(json.dumps({"health": health, "card": card}, ensure_ascii=False, indent=2))
        elif args.komut == "tasks":
            # Uzak sunucudaki görev listesi (task/list).
            r = rpc(args.host, "task/list", {}, args.token, args.port,
                    sign=sign, conv_id=args.conv)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.komut == "stream":
            # Canlı SSE akışı: H1 → H3 mesaj akışını dinle
            url = f"http://{args.host}:{args.port}/stream?message={urllib.parse.quote(args.gorev or 'selam')}&seconds={args.seconds}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=args.seconds + 10) as resp:
                for line in resp:
                    if line.strip():
                        print(line.decode().strip())
    except Exception as e:
        print(f"HATA: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
