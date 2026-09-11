#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_memory_cmd.py — D modülü (ortak hafıza) testleri (2026-08-29)

Kapsam (PREWALK TODO 13):
  1. export: memory DIF → JSONL delta (secret RED dahil)
  2. push:   rclone copy çağrısı mock'lu (GDrive'a gerçek yazma YOK)
  3. pull/import: uzak delta → conflict_policy='preserve' (conflict/tombstone)
  4. fact_store: DIF kayıtları memory_store.db facts tablosuna INSERT OR IGNORE

KISIT: gerçek rclone/GDrive ÇALIŞTIRILMAZ — subprocess.run mock'lanır.
Kullanım: python3 test_memory_cmd.py   (pytest gerekmez, sıfır bağımlılık)
"""
import json
import os
import shutil
import subprocess
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

import sync_memory as smem
import sync_motor as motor

TMP = tempfile.mkdtemp(prefix="memcmd_test_")
PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name, detail=""):
    print(f"  ✗ FAIL: {name} {detail}")
    sys.exit(1)


def make_cfg():
    """Minimal cfg — gdrive.user_root ve identity yeterli."""
    return {
        "identity": {"user_id": "cumulusnet", "machine_id": "test-node-1"},
        "gdrive": {"user_root": "gdrive:hermes-sync/cumulusnet"},
        "machine": "H1",
    }


def make_memory_dir():
    """shared/private/quarantine namespace'lerinde örnek DIF kayıtları."""
    mdir = os.path.join(TMP, "memory")
    for ns in ("shared", "private", "quarantine"):
        os.makedirs(os.path.join(mdir, ns), exist_ok=True)
    # shared: normal kayıt
    rec1 = {"record_id": "f1", "namespace": "shared",
            "subject": "project-x", "predicate": "build_command",
            "value": "make release", "value_type": "string",
            "revision": 1, "hlc": "1000.0001-test-node-1",
            "source": {"agent_id": "cumulusnet", "node_id": "test-node-1"}}
    with open(os.path.join(mdir, "shared", "f1.json"), "w") as f:
        json.dump(rec1, f, ensure_ascii=False)
    # private: fact'e yazılmamalı (sadece shared hub'a gider)
    rec2 = {"record_id": "p1", "namespace": "private",
            "subject": "local-note", "predicate": "note",
            "value": "h1 local", "value_type": "string",
            "revision": 1, "hlc": "1001.0001-test-node-1",
            "source": {"agent_id": "cumulusnet", "node_id": "test-node-1"}}
    with open(os.path.join(mdir, "private", "p1.json"), "w") as f:
        json.dump(rec2, f, ensure_ascii=False)
    return mdir


# ─── 1. EXPORT ────────────────────────────────────────────────
print("── EXPORT ──")
mdir = make_memory_dir()
cfg = make_cfg()
delta_path = motor.memory_export(mdir, cfg, dry_run=False)
if not delta_path or not os.path.exists(delta_path):
    fail("export delta üretilmedi", str(delta_path))
ok("export: JSONL delta oluştu")

with open(delta_path) as f:
    lines = [json.loads(l) for l in f if l.strip()]
if len(lines) != 2:
    fail("export kayıt sayısı (2 namespace içermeli)", str(len(lines)))
ok(f"export: {len(lines)} kayıt delta'da")

# secret RED: value içinde sk- anahtarı → ValueError
bad_dir = os.path.join(TMP, "badmem")
os.makedirs(os.path.join(bad_dir, "shared"), exist_ok=True)
bad = {"record_id": "sec1", "namespace": "shared",
       "subject": "x", "predicate": "key", "value": "sk-abcdefghijklmnopqrstuvwxyz",
       "revision": 1, "hlc": "1.0-test"}
with open(os.path.join(bad_dir, "shared", "sec1.json"), "w") as f:
    json.dump(bad, f, ensure_ascii=False)
try:
    res = motor.memory_export(bad_dir, cfg, dry_run=False)
    if res is not None:
        fail("secret taraması RED etmedi (delta döndü)", str(res))
    ok("export: secret hit → None (RED, delta üretilmez)")
except ValueError:
    ok("export: secret hit → ValueError (RED)")

# ─── 2. PUSH (mock) ───────────────────────────────────────────
print("── PUSH (mock rclone) ──")
calls = []


class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def fake_run(cmd, **kw):
    calls.append(cmd)
    if cmd[0] == "rclone" and cmd[1] == "copy":
        # push: gerçek kopyalama yok — sadece rc 0
        return FakeProc(0)
    if cmd[0] == "rclone" and cmd[1] == "lsf":
        # hub'da 1 uzak delta var (kendi push'umuz dışında: node-2 delta)
        return FakeProc(0, "20260829T000000Z-test-node-2-0.jsonl\n")
    if cmd[0] == "rclone" and cmd[1] == "copyto":
        # uzak delta indiriliyormuş gibi hedefe node-2 delta kaydı yaz
        # cmd = ["rclone", "copyto", "gdrive:.../fname.jsonl", dst]
        dst = cmd[3]
        rec = {"record_id": "f2", "namespace": "shared",
               "subject": "project-y", "predicate": "build_command",
               "value": "make test", "value_type": "string",
               "revision": 1, "hlc": "2000.0001-test-node-2",
               "source": {"agent_id": "cumulusnet", "node_id": "test-node-2"}}
        with open(dst, "w") as wf:
            wf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return FakeProc(0)
    return FakeProc(1, "", "unknown mock cmd")


orig_run = subprocess.run
subprocess.run = fake_run
try:
    pushed = motor.memory_push(cfg, delta_path, dry_run=False)
    if not pushed:
        fail("push başarısız")
    ok("push: rclone copy çağrıldı + rc 0")
    copy_calls = [c for c in calls if c[:2] == ["rclone", "copy"]]
    if not any("shared/memory" in " ".join(c) for c in copy_calls):
        fail("push hedef hub'da değil", str(copy_calls))
    ok("push: hedef gdrive:hermes-sync/cumulusnet/shared/memory")
finally:
    subprocess.run = orig_run

# ─── 3. PULL/IMPORT (mock) ────────────────────────────────────
print("── PULL/IMPORT (mock) ──")
calls.clear()
subprocess.run = fake_run
try:
    applied = motor.memory_pull_import(cfg, mdir, dry_run=False)
    if applied != 1:
        fail("import uygulanan kayıt (node-2 f2)", str(applied))
    ok(f"pull/import: {applied} uzak kayıt uygulandı")
    f2_path = os.path.join(mdir, "shared", "f2.json")
    if not os.path.exists(f2_path):
        fail("f2 import edilmedi")
    ok("import: uzak f2 kaydı shared/ altına yazıldı")
finally:
    subprocess.run = orig_run

# conflict senaryosu: aynı revision + farklı hlc → .conflict korunur
conf_dir = os.path.join(TMP, "confmem")
os.makedirs(os.path.join(conf_dir, "shared"), exist_ok=True)
conf_local = {"record_id": "c1", "namespace": "shared",
              "subject": "dup", "predicate": "value", "value": "local",
              "revision": 2, "hlc": "3000.0001-node-1"}
conf_remote = {"record_id": "c1", "namespace": "shared",
               "subject": "dup", "predicate": "value", "value": "remote",
               "revision": 2, "hlc": "3000.0001-node-2"}
with open(os.path.join(conf_dir, "shared", "c1.json"), "w") as f:
    json.dump(conf_local, f, ensure_ascii=False)
dl = os.path.join(TMP, "conflict_delta.jsonl")
with open(dl, "w") as f:
    f.write(json.dumps(conf_remote, ensure_ascii=False) + "\n")
res = smem.import_memory_delta(conf_dir, dl, "test-node-1",
                               conflict_policy="preserve")
if res["conflicts"] != 1:
    fail("conflict korunmadı", str(res))
ok("import: eşit revision + farklı hlc → .conflict korundu")

# ─── 4. FACT_STORE ────────────────────────────────────────────
print("── FACT_STORE ──")
db = os.path.join(TMP, "memory_store.db")
import sqlite3
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE facts (fact_id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " content TEXT UNIQUE, category TEXT DEFAULT 'general',"
             " tags TEXT DEFAULT '', trust_score REAL DEFAULT 0.5,"
             " retrieval_count INTEGER DEFAULT 0, helpful_count INTEGER DEFAULT 0,"
             " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
             " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
             " hrr_vector BLOB)")
conn.commit()
conn.close()

# memory dir'i DIF'lerle doldur (shared f1 + f2 import edilmişti)
added = motor.memory_to_fact_store(mdir, dry_run=False, db_path=db)
if added < 2:
    fail("fact_store eklenen kayıt", str(added))
ok(f"fact_store: +{added} kayıt (f1 + f2)")

# idempotent: ikinci çağrı aynı içeriği tekrar eklemez (UNIQUE)
added2 = motor.memory_to_fact_store(mdir, dry_run=False, db_path=db)
if added2 != 0:
    fail("fact_store dedup çalışmadı (tekrar ekledi)", str(added2))
ok("fact_store: INSERT OR IGNORE dedup (ikinci koşu 0)")

# dry-run: gerçek DB'ye yazmaz
conn = sqlite3.connect(db)
cnt_before = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
conn.close()
motor.memory_to_fact_store(mdir, dry_run=True, db_path=db)
conn = sqlite3.connect(db)
cnt_after = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
conn.close()
if cnt_before != cnt_after:
    fail("dry-run DB'ye yazdı", f"{cnt_before}→{cnt_after}")
ok("fact_store: dry-run yazmaz")

# ─── 5. CMD_MEMORY (mock) — tam akış ──────────────────────────
print("── CMD_MEMORY (mock) ──")
calls.clear()
subprocess.run = fake_run
try:
    rc = motor.cmd_memory(cfg, dry_run=False, memory_dir=mdir, memory_db=db)
    if rc != 0:
        fail("cmd_memory rc", str(rc))
    ok("cmd_memory: tam akış rc=0 (export→push→pull/import→fact)")
    lsf_calls = [c for c in calls if c[:2] == ["rclone", "lsf"]]
    copy_calls = [c for c in calls if c[:2] == ["rclone", "copy"]]
    if not lsf_calls or not copy_calls:
        fail("cmd_memory hub etkileşimi eksik", f"lsf={len(lsf_calls)} copy={len(copy_calls)}")
    ok(f"cmd_memory: lsf={len(lsf_calls)} + copy={len(copy_calls)} hub çağrısı")
finally:
    subprocess.run = orig_run

# dry-run cmd_memory: hiçbir şey yazmaz (delta None döner, push atlanır)
rc_dry = motor.cmd_memory(cfg, dry_run=True, memory_dir=mdir, memory_db=db)
if rc_dry != 0:
    fail("cmd_memory dry-run rc", str(rc_dry))
ok("cmd_memory: dry-run rc=0")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n✅ TÜM TESTLER GEÇTİ — {PASS} PASS")
sys.exit(0)
