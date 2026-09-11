#!/usr/bin/env python3
"""test_sync_memory.py — çoklu-ajan ortak hafıza testleri (2026-08-15)"""
import copy
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Modülleri barındıran dizini işaret dosyasıyla bul: betik depo kökünde veya
# tests/manual/ altında olabilir; sabit dirname sayısı kırılgan (private kopyada
# bir fazla dirname yanlış dizini gösteriyordu — 11 Eyl 2026).
_KOK = HERE
for _ in range(4):
    if os.path.isfile(os.path.join(_KOK, "sync_motor.py")):
        break
    _KOK = os.path.dirname(_KOK)
for _p in (HERE, _KOK, os.path.join(_KOK, "synclave")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import sync_memory as sm

TMP = tempfile.mkdtemp(prefix="syncmem_test_")
PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


# ─── 1. Export/Import delta ───
mem = os.path.join(TMP, "memory")
for ns in ("shared", "private", "quarantine"):
    os.makedirs(os.path.join(mem, ns), exist_ok=True)
with open(os.path.join(mem, "shared", "f1.json"), "w") as f:
    json.dump({"record_id": "f1", "namespace": "shared",
               "subject": "project-x", "predicate": "build_command",
               "value": "make release", "value_type": "string",
               "revision": 1, "hlc": "1.0-H1"}, f)
exp = sm.export_memory_delta(mem, "H1", "hermes-h1")
assert exp["records"] == 1 and os.path.exists(exp["delta"])
ok(f"export delta ({exp['records']} kayıt)")

# import (H2'ye)
mem2 = os.path.join(TMP, "memory2")
for ns in ("shared", "private", "quarantine"):
    os.makedirs(os.path.join(mem2, ns), exist_ok=True)
imp = sm.import_memory_delta(mem2, exp["delta"], "H2")
assert imp["applied"] == 1 and imp["conflicts"] == 0
assert os.path.exists(os.path.join(mem2, "shared", "f1.json"))
ok("import delta (H2'ye)")

# ─── 2. Secret tarama RED ───
bad = {"record_id": "s1", "value": "sk-abcdefghijklmnop1234567890"}
scan = sm.scan_payload_for_secrets(bad)
assert not scan["ok"] and scan["hits"]
ok(f"secret tarama RED ({scan['hits']})")

# ─── 3. Tombstone ───
tomb = {"record_id": "f1", "namespace": "shared", "tombstone": True,
        "hlc": "2.0-H2", "revision": 2}
with open(os.path.join(mem2, "deltas_tomb.jsonl"), "w") as f:
    f.write(json.dumps(tomb) + "\n")
imp2 = sm.import_memory_delta(mem2, os.path.join(mem2, "deltas_tomb.jsonl"), "H2")
assert imp2["tombstones"] == 1
assert not os.path.exists(os.path.join(mem2, "shared", "f1.json"))
ok("tombstone (silme fiziksel değil)")

# ─── 4. Audit hash-chain ───
aud = os.path.join(TMP, "audit")
h1 = sm.append_audit_event(aud, {"node_id": "H1", "agent_id": "hermes-h1",
                                 "node_name": "kernel", "path": "src/a.c",
                                 "old_sha256": "0"*64, "new_sha256": "1"*64,
                                 "event_type": "write", "result": "committed"})
h2 = sm.append_audit_event(aud, {"node_id": "H2", "agent_id": "hermes-h2",
                                 "node_name": "kernel", "path": "src/b.c",
                                 "old_sha256": "1"*64, "new_sha256": "2"*64,
                                 "event_type": "write", "result": "committed"})
assert h1 != h2
ver = sm.verify_audit_chain(aud)
assert ver["ok"] and ver["events"] == 2
ok("audit hash-chain (2 olay)")

# zincir bozulma tespiti
logp = os.path.join(aud, os.listdir(aud)[0])
with open(logp) as f:
    lines = f.readlines()
lines[0] = lines[0].replace('"new_sha256": "1"*64', '"new_sha256": "9"*64')
# gerçek bir değişiklik yap: sha değerini değiştir
ev0 = json.loads(lines[0])
ev0["new_sha256"] = "f" * 64
lines[0] = json.dumps(ev0) + "\n"
with open(logp, "w") as f:
    f.writelines(lines)
ver2 = sm.verify_audit_chain(aud)
assert not ver2["ok"]
ok("audit zincir bozulma tespiti")

# ─── 5. Skill envanteri ───
sk = os.path.join(TMP, "skills")
os.makedirs(os.path.join(sk, "mesh"), exist_ok=True)
with open(os.path.join(sk, "mesh", "SKILL.md"), "w") as f:
    f.write("# mesh skill v1")
with open(os.path.join(sk, "mesh", "skill.json"), "w") as f:
    json.dump({"version": "1.4.2"}, f)
inv = sm.scan_skill_inventory(sk)
assert len(inv) == 1 and inv[0]["skill_id"] == "mesh"
ok("skill envanter tarama")

# remote'ta yeni skill + mesh farklı sürüm
# 29 Ağu 2026 FIX (H2): `inv + [...]` SHALLOW copy — remote[0] ile inv[0] AYNI dict
# nesnesiydi, satır altındaki hash ataması ikisini birden değiştirdiği için mesh
# hash'leri eşit kalıyor ve "mismatch" hiç oluşmuyordu (test hatası; üretim kodu
# compare_skill_inventories doğru çalışıyor — ayrı dict'lerle mismatch+missing verir).
remote = copy.deepcopy(inv) + [{"skill_id": "vital", "version": "2.0.0",
                 "content_sha256": "ab" * 32, "manifest_path": "vital/SKILL.md"}]
remote[0]["content_sha256"] = "cd" * 32  # mesh hash farklı
acts = sm.compare_skill_inventories(inv, remote)
assert any(a["status"] == "missing" for a in acts)
assert any(a["status"] == "mismatch" for a in acts)
ok("skill karşılaştırma (missing + mismatch)")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nsync_memory: {PASS} test PASS")
