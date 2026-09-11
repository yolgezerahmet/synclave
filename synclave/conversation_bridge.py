#!/usr/bin/env python3
"""
conversation_bridge.py — Hermes state.db → ajan sohbet defteri köprüsü (v1.0)

AMAÇ (30 Ağu 2026 kullanıcı talebi — "sohbetler kaybolmadan eş zamanlı iş"):
  Hermes'in TÜM kullanıcı sohbetleri (telegram/cli/whatsapp/web) kalıcı,
  karışmaz sohbet ID'leriyle ajan kimlik defterine (conversations.db) akar.
  - Her oturum → kullanıcı sohbeti: u.<agent8>.<kanal>.<peer8>.<ulid>
  - Her mesaj → <conv_id>#<seq> (içerik SAKLANMAZ, sadece sha256 + boyut)
  - Watermark: son işlenen state.db mesaj id'si kaydedilir → incremental
  - Hermes state.db'ye DOKUNMAZ (sadece okur); defter ayrı DB

ÇALIŞMA:
  python3 conversation_bridge.py            # watermark'tan itibaren işle
  python3 conversation_bridge.py --full     # TÜM geçmişi işle (ilk kurulum)
  python3 conversation_bridge.py --dry-run  # ne yapılacağını göster

CRON (no_agent, 15 dk):
  python3 conversation_bridge.py
"""
from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # Windows — fcntl yok
    fcntl = None
    import msvcrt
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import agent_identity as AI
except Exception as e:
    print(f"agent_identity yüklenemedi: {e}")
    sys.exit(1)

def _state_db():
    # Her çağrıda okunur — testler env'i değiştirip import'tan önce
    # çalışamayabilir (modül import anında sabitlenirse GERÇEK DB'ye bağlanır).
    return os.environ.get("HERMES_STATE_DB", "~/.hermes/state.db")


def _watermark():
    return os.environ.get("BRIDGE_WATERMARK", "~/.hermes/state/bridge_watermark.json")
# source → kanal adı; user_id → görünen kullanıcı adı (gönderen tarafı eşleme)
CHANNEL_MAP = {"telegram": "telegram", "cli": "cli", "whatsapp": "whatsapp",
               "web": "web", "slack": "slack", "discord": "discord", "origin": "cli"}
KNOWN_USERS = {  # <kanal>:<user_id> → görünen ad (peer etiketi değil, label)
    "telegram:729504083": "Ahmet",
    "telegram:0": "bilinmeyen",
    "cli:root": "Ahmet",
    "cli:": "Ahmet",
}
BATCH = 500
SLEEP_PER_BATCH = 0.0
_LOCK_FILE = os.path.expanduser("~/.hermes/state/bridge.lock")


def _acquire_lock():  # -> int | None
    """Tek-instance kilidi — iki köprü aynı anda conversations.db'ye yazmasın
    (cron + manuel çakışması SQLite kilit kilitlenmesine yol açıyordu)."""
    try:
        Path(_LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        if fcntl is not None:  # Linux/macOS
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # Windows — msvcrt ile dosyayı kilitle
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return fd
    except OSError:
        return None


def _load_wm() -> dict:
    p = Path(_watermark()).expanduser()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_wm(wm: dict):
    p = Path(_watermark()).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(wm, ensure_ascii=False, indent=1), encoding="utf-8")


def _sessions(conn: sqlite3.Connection) -> dict:
    """session_id → (source, user_id, chat_id, title)"""
    # NOT: Hermes state.db şema varyantları arasında 'chat_id' sütunu HER sürümde
    # yoktur (bazı sürümlerde user_id tek kanal kimliğidir). Eksikse boş geç.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    sel = ", ".join(c if c in cols else "'' AS %s" % c
                    for c in ("id", "source", "user_id", "chat_id", "title"))
    rows = conn.execute("SELECT %s FROM sessions" % sel).fetchall()
    out = {}
    for sid, source, uid, chat_id, title in rows:
        src = (source or "cli").lower()
        if src not in CHANNEL_MAP:
            src = "cli"
        out[sid] = (src, str(uid or ""), str(chat_id or ""), title or "")
    return out


def _label_for(src: str, uid: str) -> str:
    return KNOWN_USERS.get(f"{src}:{uid}", uid or src)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="tüm geçmişi işle")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    lock_fd = _acquire_lock()
    if lock_fd is None:
        print("[köprü] başka bir köprü çalışıyor — bu koşu ATLANDI", file=sys.stderr)
        return 0

    ident = AI.AgentIdentity.load_or_create()
    print(f"[köprü] kimlik: {ident.agent_id} | {ident.runtime}")

    state = sqlite3.connect(str(Path(_state_db()).expanduser()), timeout=20)
    state.row_factory = sqlite3.Row
    wm = _load_wm()
    sess = _sessions(state)

    # watermark: session başına son işlenen mesaj id
    # full modda yok say
    if a.full:
        wm = {}

    # hangi session'ları işleyeceğiz: watermark'ta olmayan veya kısmi olanlar
    to_proc = {sid: (sess.get(sid, ("cli", "", "", ""))) for sid in sess}
    # session'lar arasında session_id kayıp? (silinmiş) — yine de işle

    total_msg = 0
    total_conv = 0
    # ── v1.1: toplu INSERT (performans) — log_message başına commit yerine
    # tek transaction + executemany. conversations.db şemasıyla BİREBİR aynı.
    conn = AI._conn(ident.runtime)  # cache'li bağlantı (agent_identity)
    cur = conn.cursor()
    # ── v1.2: SESSION-BAŞINA-SORGU YOK — tüm mesajlar TEK geçişte çekilir.
    #    (12.6K session × 0.037s = ~466s idi; tek sorgu + fetchmany = saniyeler)
    # 1) Her session için conv_id haritası (idempotent open_conversation)
    conv_map: dict[str, str] = {}
    peer_map: dict[str, str] = {}
    for sid, (src, uid, chat_id, title) in to_proc.items():
        peer = uid or "unknown"
        try:
            conv_map[sid] = AI.open_conversation("user", peer, src,
                                                 _label_for(src, uid), identity=ident)
            peer_map[sid] = peer
        except Exception as e:
            print(f"  ! {sid}: conv açılamadı: {e}")
    total_conv = len(conv_map)
    # 2) Mesajları tek sorguda, tüm session'lar için (id sıralı)
    state.row_factory = sqlite3.Row
    # NOT: 'active' sütunu bazı Hermes sürümlerinde yoktur — varsa filtrele, yoksa atla.
    mcols = {r[1] for r in state.execute("PRAGMA table_info(messages)")}
    active_f = "AND m.active=1" if "active" in mcols else ""
    q = state.execute(
        "SELECT m.id, m.session_id, m.role, m.content, m.timestamp "
        "FROM messages m WHERE m.content IS NOT NULL %s "
        "ORDER BY m.id ASC" % active_f)
    # 3) Session başına watermark filtresi + CONV başına seq takibi.
    #    KRİTİK: binlerce session AYNI kullanıcı+kanala (aynı conv_id'ye) akar;
    #    seq conv genelinde monoton olmalı — yoksa msg_id çakışır ve
    #    INSERT OR REPLACE önceki mesajları ezer (veri kaybı!).
    seq_of: dict[str, int] = {}
    for cid in set(conv_map.values()):
        row = cur.execute("SELECT msg_count FROM conversations WHERE conv_id=?",
                          (cid,)).fetchone()
        seq_of[cid] = int(row[0]) if row and row[0] else 0
    batch_out = []
    last_commit = 0
    while True:
        rows = q.fetchmany(BATCH)
        if not rows:
            break
        for r in rows:
            sid = r["session_id"]
            if sid not in conv_map:
                continue
            if r["id"] <= wm.get(sid, 0):
                continue
            role = (r["role"] or "").lower()
            if role not in ("user", "assistant"):
                continue
            direction = "in" if role == "user" else "out"
            raw = (r["content"] or "")[:200000]
            conv_id = conv_map[sid]
            seq_of[conv_id] = seq_of.get(conv_id, 0) + 1
            seq = seq_of[conv_id]
            batch_out.append((f"{conv_id}#{seq}", conv_id, seq, direction,
                              r["timestamp"], peer_map.get(sid, ""),
                              hashlib.sha256(raw.encode(errors="replace")).hexdigest(),
                              len(raw.encode(errors="replace"))))
            wm[sid] = r["id"]
            total_msg += 1
        if batch_out:
            cur.executemany(
                "INSERT OR REPLACE INTO messages(msg_id,conv_id,seq,direction,ts,peer_id,sha256,bytes,meta) "
                "VALUES(?,?,?,?,?,?,?,?,NULL)", batch_out)
            batch_out = []
        # conversation msg_count'larını toplu güncelle (seq_of'tan)
        if seq_of:
            cur.executemany(
                "UPDATE conversations SET msg_count=? WHERE conv_id=?",
                [(s, cid) for cid, s in seq_of.items()])
        if total_msg - last_commit >= 20000:
            conn.commit()
            _save_wm(wm)
            last_commit = total_msg
            print(f"  ... {total_msg} mesaj işlendi")
        time.sleep(SLEEP_PER_BATCH)
    conn.commit()

    _save_wm(wm)
    print(f"[köprü] {total_msg} mesaj, {total_conv} oturum batch, "
          f"defter: {ident.dir / 'conversations.db'}")
    try:
        os.close(lock_fd)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
