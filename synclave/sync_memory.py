#!/usr/bin/env python3
"""
sync_memory.py — Çoklu-Ajan Ortak Hafıza + Node Farkındalığı (v0.1)
====================================================================
GPT-5.6 tasarımı (2026-08-15, /tmp/review_sync_memory_ocean.txt) çekirdeği.

Senaryolar:
- H1 (Hermes, Linux VPS) ↔ H2 (Hermes, Windows) ↔ OpenClaw (ayrı ajan)
- Her ajan bir "node"; aynı konu/kod üzerinde node-farkında çalışır
- Ortak hafıza: memory DIF'leri JSONL (canlı DB senkronize EDİLMEZ)
- Audit: append-only JSONL + hash-chain (kim ne yaptı — değiştirilemez iz)
- Skill envanteri: sürüm+SHA256 karşılaştırma (farklılıklar giderilir)

İlkeler (GPT-5.6):
1. hermes-full canlı senkron DEĞİL — ayrı hermes-memory node (shared/private)
2. Canlı DB dosyası değil, MANTIKSAL DELTA'lar (JSONL)
3. Çakışma: son-yazan kazanır DEĞİL — preserve (.conflict~node~ts) + audit
4. Tombstone gerekli (silme fiziksel değil — yoksa kayıt geri gelir)
5. Secret: allowlist alan modeli — tarama başarısızsa DUR
6. Saat: HLC/Lamport mantıksal saat (duvar saati sıralayıcı değil)
"""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:          # Windows (H2)
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

# ─── Secret taraması (GPT-5.6: regex yeterli değil — allowlist + tarama) ───
SECRET_FIELD_RE = re.compile(
    r"(token|api[_-]?key|secret|password|credential|private[_-]?key|"
    r"access[_-]?token|auth[_-]?token|bearer|client[_-]?secret)",
    re.IGNORECASE)
ALLOWED_VALUE_FIELDS = {"subject", "predicate", "value", "value_type"}


def scan_payload_for_secrets(record: dict) -> dict:
    """Secret taraması: RECURSIVE — tüm alan adları + tüm string değerler.

    29 Ağu 2026 FIX (OceanAPI denetim bulgusu #2): önceki sürüm yalnız üst
    seviye alan adlarını ve `value` alanını tarıyordu; iç içe dict/list
    değerler ve ALLOWED_VALUE_FIELDS dışındaki alanlar (source, metadata
    vb.) içinde gizlenen secret'lar kaçabiliyordu. Artık ağaç tam gezilir:
    her alan ADI SECRET_FIELD_RE ile, her string DEĞER kalıp regex'leriyle
    taranır. Hit → RED (ok=False) — export hiçbir şey yazmaz (fail-closed).
    """
    hits = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                key = f"{path}.{k}" if path else str(k)
                if SECRET_FIELD_RE.search(str(k)):
                    hits.append(key)
                walk(v, key)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for pat in (r"sk-[A-Za-z0-9]{16,}", r"ghp_[A-Za-z0-9]{30,}",
                        r"AKIA[0-9A-Z]{16}", r"Bearer [A-Za-z0-9._-]+"):
                if re.search(pat, node):
                    hits.append(f"{path}:{pat[:10]}...")
                    break

    walk(record or {})
    return {"hits": sorted(set(hits)), "ok": len(hits) == 0}


# ─── HLC (Hybrid Logical Clock) — duvar saati sıralayıcı DEĞİL ───
class HLC:
    def __init__(self, node_id: str):
        self.node_id = node_id
        self.pt = 0  # fiziksel zaman ms
        self.c = 0   # mantıksal sayaç

    def now(self) -> str:
        phys = int(time.time() * 1000)
        if phys > self.pt:
            self.pt, self.c = phys, 0
        else:
            self.c += 1
        return f"{self.pt}.{self.c:04d}-{self.node_id}"


# ─── Ortak Hafıza Delta ────────────────────────────────────────────────
MEMORY_NAMESPACES = ("shared", "private", "quarantine")


def export_memory_delta(memory_dir: str, node_id: str, agent_id: str,
                        since_seq: int = 0) -> dict:
    """Memory kayıtlarını JSONL delta olarak dışa aktar.
    memory_dir/snapshots/ + memory_dir/deltas/ + memory_dir/manifests/
    Kayıtlar: {record_id, namespace, subject, predicate, value, value_type,
               source:{agent_id,node_id}, hlc, revision, tombstone}"""
    delta = []
    for ns in MEMORY_NAMESPACES:
        ns_dir = os.path.join(memory_dir, ns)
        if not os.path.isdir(ns_dir):
            continue
        for fn in sorted(os.listdir(ns_dir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(ns_dir, fn)) as f:
                rec = json.load(f)
            rec.setdefault("source", {})
            rec["source"].setdefault("agent_id", agent_id)
            rec["source"].setdefault("node_id", node_id)
            rec.setdefault("revision", 1)
            delta.append(rec)
    # secret taraması: hit varsa delta RED (GPT-5.6: warning ile devam yok)
    for rec in delta:
        scan = scan_payload_for_secrets(rec)
        if not scan["ok"]:
            raise ValueError(f"SECRET: {rec.get('record_id')} alan {scan['hits']}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # 29 Ağu FIX (OceanAPI #1): aynı saniyedeki iki export çakışmasın —
    # µs + uuid soneki overwrite'ı imkânsız kılar; sıralama ts önekinden korunur
    out = os.path.join(memory_dir, "deltas",
                       f"{ts}-{node_id}-{since_seq}-{uuid.uuid4().hex[:6]}.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        for rec in delta:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"delta": out, "records": len(delta), "node_id": node_id}


def import_memory_delta(memory_dir: str, delta_path: str,
                        node_id: str, conflict_policy: str = "preserve") -> dict:
    """Delta uygula — non-destructive (.conflict~node~ts koru).
    Aynı record_id: revision karşılaştır — yüksek revision kazanır,
    eşitse ikisini koru (çakışma). tombstone=true → kaydı kaldır (fiziksel
    silme değil — kaldır/ignore).

    29 Ağu 2026 (OceanAPI denetim bulguları #3/#4/#6 + 2. tur #1/#2/#3):
    - Gelen her kayıt secret taramasından geçer (fail-closed: hit → atla,
      rejected_secret sayacı). Export RED'le hiç göndermez ama bir node
      bozulduysa/ele geçirildiyse import tarafı da savunma yapar.
    - Tombstone revision karşılaştırmalı: mevcut kayıt daha yeni ise eski
      silme uygulanmaz (veri kaybı önlendi). Eşit revision + farklı hlc'de
      mevcut kayıt korunur (silme kaybeder — conflict sayılır).
    - TOMBSTONE INDEX (tombstones/<rid>.json): silme işlemi hedef dosya
      olmasa da kalıcı olarak kaydedilir. Normal yazımda index kontrol
      edilir — index.revision >= gelen.revision ise kayıt YENİDEN
      OLUŞTURULMAZ (resurrection/veri kaybı önlendi). Daha yüksek
      revision'lu yeni kayıt → meşru diriltme: index temizlenir, yazılır.
    - Conflict/tombstone dosya adları µs hassasiyetli — aynı saniyede
      birden çok çakışma birbirinin üzerine yazamaz.
    """
    applied = conflicts = tombstones = rejected_secret = resurrected = 0
    if not os.path.exists(delta_path):
        return {"applied": 0, "conflicts": 0, "error": "delta yok"}
    with open(delta_path) as f:
        for line in f:
            rec = json.loads(line)
            rid = rec.get("record_id")
            ns = rec.get("namespace", "shared")
            if ns not in MEMORY_NAMESPACES:
                continue
            # import tarafı secret savunması (OceanAPI #3)
            sec = scan_payload_for_secrets(rec)
            if not sec["ok"]:
                rejected_secret += 1
                continue
            ns_dir = os.path.join(memory_dir, ns)
            os.makedirs(ns_dir, exist_ok=True)
            target = os.path.join(ns_dir, f"{rid}.json")
            tomb_idx_dir = os.path.join(memory_dir, "tombstones", ns)
            tomb_idx = os.path.join(tomb_idx_dir, f"{rid}.json")
            rec_rev = rec.get("revision", 0)

            def _tomb_idx_state():
                if os.path.exists(tomb_idx):
                    with open(tomb_idx) as tf:
                        return json.load(tf)
                return {"revision": 0, "hlc": None}

            # ── TOMBSTONE işle ──
            if rec.get("tombstone"):
                # OceanAPI #4: eski/gecikmiş tombstone daha yeni kaydı silmesin
                cur_rev = 0
                cur_hlc = None
                if os.path.exists(target):
                    with open(target) as ef:
                        existing = json.load(ef)
                    cur_rev = existing.get("revision", 0)
                    cur_hlc = existing.get("hlc")
                if cur_rev > rec_rev:
                    continue  # eski silme — yeni kayıt korunur
                # eşit revision + farklı hlc → çakışma: mevcut kayıt KORUNUR
                # (silme kaybeder; tombstone index'e yazılır ki daha eski bir
                #  kayıt bu id'yi diriltemesin — 2. tur #2)
                if cur_rev == rec_rev and cur_hlc and cur_hlc != rec.get("hlc"):
                    conflicts += 1
                    os.makedirs(tomb_idx_dir, exist_ok=True)
                    with open(tomb_idx, "w") as tf:
                        json.dump({"record_id": rid, "namespace": ns,
                                   "revision": rec_rev, "hlc": rec.get("hlc"),
                                   "tombstone": True}, tf, ensure_ascii=False)
                    continue
                if os.path.exists(target):
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    os.replace(target, f"{target}.tombstone.{node_id}.{ts}")
                # kalıcı tombstone index — hedef dosya olmasa bile (2. tur #1)
                os.makedirs(tomb_idx_dir, exist_ok=True)
                with open(tomb_idx, "w") as tf:
                    json.dump({"record_id": rid, "namespace": ns,
                               "revision": rec_rev, "hlc": rec.get("hlc"),
                               "tombstone": True}, tf, ensure_ascii=False)
                tombstones += 1
                continue

            # ── NORMAL KAYIT ──
            # resurrection önleme (2. tur #3): bu id silindiyse (index) ve
            # gelen kayıt daha eski/eşit ise YENİDEN OLUŞTURMA
            tstate = _tomb_idx_state()
            if tstate.get("tombstone") and tstate.get("revision", 0) >= rec_rev:
                resurrected += 1
                continue
            if os.path.exists(target):
                with open(target) as ef:
                    existing = json.load(ef)
                if existing.get("revision", 0) > rec_rev:
                    applied += 0  # eski delta — atla
                    continue
                if existing.get("revision", 0) == rec_rev \
                        and existing.get("hlc") != rec.get("hlc"):
                    # çakışma — ikisini koru (µs dosya adı: overwrite yok)
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    os.replace(target, f"{target}.conflict.{node_id}.{ts}")
                    conflicts += 1
            # meşru diriltme: yeni revision > tombstone index → index temizle
            if tstate.get("tombstone") and tstate.get("revision", 0) < rec_rev:
                if os.path.exists(tomb_idx):
                    os.remove(tomb_idx)
            with open(target, "w") as wf:
                json.dump(rec, wf, ensure_ascii=False, indent=1)
            applied += 1
    return {"applied": applied, "conflicts": conflicts,
            "tombstones": tombstones, "rejected_secret": rejected_secret,
            "resurrected": resurrected}


# ─── Audit Log (JSONL + hash-chain — kim ne yaptı, değiştirilemez) ───
AUDIT_REQUIRED = ("event_id", "timestamp_utc", "node_id", "agent_id",
                  "operation_id", "node_name", "path", "old_sha256",
                  "new_sha256", "event_type", "result")


def audit_log_path(audit_dir: str) -> str:
    """Bugünkü audit log dosyasının TAM yolu (günlük rotasyon: YYYY-MM-DD.jsonl).

    Neden ayrı yardımcı (14 Eyl 2026 — iki kırmızı kapı):
    `append_audit_event` log dosyasının YANINA kalıcı bir kilit dosyası bırakır
    (`<tarih>.jsonl.lock`, 29 Ağu atomik zincir düzeltmesi). Tüketiciler log
    dosyasını `os.listdir(audit_dir)[0]` ile keşfederse liste sırası dosya
    sistemine bağlı olduğundan bazen BOŞ kilit dosyası seçilir:
    (a) `readlines()` boş → IndexError, (b) `_audit_last_hash` boş dosyada
    "0"*64 döndürür → zincir doğru olsa da FAIL. Kapı KARARSIZDI (aynı kod bir
    makinede yeşil, diğerinde kırmızı — CI'da rastgele kırmızı). Yol artık tek
    kaynaktan alınır, dizin listeleme YOKTUR. Ayrıca aynı ifade iki fonksiyonda
    kopyalanmıştı (sapma riski) — tek kaynak.

    Dizin sözleşmesi: `<audit_dir>/<YYYY-MM-DD>.jsonl` (log) + aynı günün
    `<YYYY-MM-DD>.jsonl.lock` (kilit, BOŞ). Audit dizinini tarayan tüketiciler
    `.lock` dosyasını log sanmamalıdır — yol buradan alınır.
    """
    return os.path.join(audit_dir,
                        datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")


def append_audit_event(audit_dir: str, event: dict) -> str:
    """Audit olayı ekle — hash-chain (previous_event_hash + event_hash).

    29 Ağu 2026 FIX (OceanAPI #7): önceki sürüm "son hash'i oku" + "yaz"ı
    kilitsiz iki ayrı işlem yapıyordu — aynı makinede eşzamanlı iki süreç
    aynı prev_hash okuyabilir ve zincir kırılırdı. Artık okuma+yazma
    LOCK dosyası üzerinde (fcntl.flock / msvcrt.locking) atomik yapılır.
    """
    os.makedirs(audit_dir, exist_ok=True)
    log_path = audit_log_path(audit_dir)
    event.setdefault("event_id", str(uuid.uuid4()))
    event.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat() + "Z")
    event.setdefault("operation_id", str(uuid.uuid4()))
    # previous hash: log'un son satırından
    # 29 Ağu 2026 FIX (H2 bulgusu): eski "dosya sonundan geri sar" algoritması
    # TEK SATIRLI dosyada `\n` bulamayıp seek(-2) ile dosya başını aşıyor ve
    # OSError[Errno 22] fırlatıyordu → except prev_hash'i "0"*64'e sıfırlıyor,
    # 2. olay zinciri kırıyordu (verify_audit_chain "zincir kırıldı" der).
    # Audit log günlük dosya (YYYY-MM-DD.jsonl) olduğundan HER GÜNÜN ilk iki
    # olayı bu hataya düşüyordu. LF/CRLF farkından bağımsız — H1/H3 de etkilenir.
    # Audit log'lar günlük ve küçük; tamamını okumak güvenli ve doğru.
    lock_path = log_path + ".lock"
    prev_hash = "0" * 64
    if fcntl is not None:
        with open(lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                prev_hash = _audit_last_hash(log_path)
                event["previous_event_hash"] = prev_hash
                event["event_hash"] = _audit_event_hash(prev_hash, event)
                _audit_append_line(log_path, event)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    elif msvcrt is not None:
        # 29 Ağu FIX 2. tur (OceanAPI #4/#5): msvcrt.locking MEVCUT dosya
        # pozisyonunu kilitler — append modunda pozisyon EOF olduğundan
        # süreçler farklı byte'ları kilitleyip birbirini dışlamazdı. Artık
        # lock dosyası önce 1 byte'a genişletilir, seek(0) ile byte 0
        # kilitlenir. Kilit alınamazsa 3 deneme sonra RuntimeError (fail-
        # closed) — kilitsiz yazım yarışı GERİ GETİRİLMEZ.
        with open(lock_path, "a+") as lf:
            lf.seek(0, os.SEEK_END)
            if lf.tell() == 0:
                lf.write(b"\x00")
                lf.flush()
            lf.seek(0)
            locked = False
            for attempt in range(3):
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05 * (attempt + 1))
            if not locked:
                raise RuntimeError(
                    f"audit kilit alınamadı: {lock_path} (fail-closed, yazılmadı)")
            try:
                prev_hash = _audit_last_hash(log_path)
                event["previous_event_hash"] = prev_hash
                event["event_hash"] = _audit_event_hash(prev_hash, event)
                _audit_append_line(log_path, event)
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        prev_hash = _audit_last_hash(log_path)
        event["previous_event_hash"] = prev_hash
        event["event_hash"] = _audit_event_hash(prev_hash, event)
        _audit_append_line(log_path, event)
    return event["event_hash"]


def _audit_last_hash(log_path: str) -> str:
    """Log dosyasının son satırındaki event_hash — yoksa '0'*64."""
    prev_hash = "0" * 64
    if os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                lines = [ln for ln in f.read().split(b"\n") if ln.strip()]
            if lines:
                last = json.loads(lines[-1].decode("utf-8"))
                prev_hash = last.get("event_hash", "0" * 64)
        except Exception:
            prev_hash = "0" * 64
    return prev_hash


def _audit_event_hash(prev_hash: str, event: dict) -> str:
    body = json.dumps({k: event.get(k) for k in AUDIT_REQUIRED},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def _audit_append_line(log_path: str, event: dict) -> None:
    # newline="\n": Windows'ta CRLF yazılmasın (satır sonu tutarlılığı)
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def verify_audit_chain(audit_dir: str, log_path: str = None) -> dict:
    """Hash zinciri doğrula — log satırı değiştirildiyse zincir kırılır.

    `log_path`: hedef dosyayı AÇIKÇA verir (varsayılan: bugünkü log).
    Neden gerekli (14 Eyl 2026, gpt-5.6-sol kritik denetimi — gün devri bulgusu):
    yazma ve doğrulama AYRI çağrılar olduğundan UTC gece yarısını aşan bir
    "yaz + doğrula" zincirinde varsayılan yol ERTESİ günün dosyasını seçer ve
    doğrulama zincir sağlam olsa da "log yok" ile kırmızı görünür. Çağıran,
    yazdığı dosyanın yolunu (`audit_log_path`) bir kez üretip buraya geçirirse
    doğrulama doğru dosyada yapılır. Varsayılan davranış DEĞİŞMEZ (bugün).
    """
    if log_path is None:
        log_path = audit_log_path(audit_dir)
    if not os.path.exists(log_path):
        return {"ok": False, "error": "log yok"}
    prev = "0" * 64
    n = 0
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            n += 1
            ev = json.loads(line)
            body = json.dumps({k: ev.get(k) for k in AUDIT_REQUIRED},
                              sort_keys=True, ensure_ascii=False)
            calc = hashlib.sha256((ev.get("previous_event_hash", "") + body).encode()).hexdigest()
            if ev.get("previous_event_hash", "0"*64) != prev or calc != ev.get("event_hash"):
                return {"ok": False, "error": "zincir kırıldı", "at": ev.get("event_id")}
            prev = ev.get("event_hash")
    return {"ok": True, "events": n}


# ─── Skill Envanteri (sürüm+SHA256 — farklılıklar giderilir) ───
def scan_skill_inventory(skill_root: str) -> list:
    """Skill envanteri: {skill_id, version, content_sha256, manifest_path}"""
    inv = []
    if not os.path.isdir(skill_root):
        return inv
    for root, _, files in os.walk(skill_root):
        if "SKILL.md" not in files:
            continue
        p = os.path.join(root, "SKILL.md")
        with open(p, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        skill_id = os.path.basename(root)
        version = "0.0.0"
        ver_f = os.path.join(root, "skill.json")
        if os.path.exists(ver_f):
            try:
                with open(ver_f) as vf:
                    version = json.load(vf).get("version", "0.0.0")
            except Exception:
                pass
        inv.append({"skill_id": skill_id, "version": version,
                    "content_sha256": h, "manifest_path": p})
    return inv


def compare_skill_inventories(local: list, remote: list) -> list:
    """Karşılaştır — eksik/farklı skill'leri raporla (mismatch)."""
    actions = []
    remote_map = {r["skill_id"]: r for r in remote}
    local_map = {l["skill_id"]: l for l in local}
    for rid, r in remote_map.items():
        if rid not in local_map:
            actions.append({"skill_id": rid, "status": "missing",
                            "remote": r})
        elif local_map[rid]["content_sha256"] != r["content_sha256"]:
            actions.append({"skill_id": rid, "status": "mismatch",
                            "local": local_map[rid], "remote": r})
    return actions


if __name__ == "__main__":
    print("sync_memory.py — çoklu-ajan ortak hafıza + node farkındalığı")
    print("Kütüphane modülü — sync_motor.py'den import edilir.")
