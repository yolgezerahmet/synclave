#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_memory_fixes.py — OceanAPI denetim bulguları kapanış testleri (2026-08-29)

Kapsam:
  1. recursive secret tarama (iç içe dict/list — OceanAPI #2)
  2. import secret savunması (rejected_secret — OceanAPI #3)
  3. eski tombstone yeni kaydı silmez (revision karşılaştırma — #4)
  4. export dosya adı µs+uuid (aynı saniye overwrite yok — #1)
  5. conflict/tombstone dosya adı µs (#6)
  6. audit append kilitli zincir (#7)
  7. cmd_memory hata yayılımı (pull/import + fact_store -1 → rc=1 — #8)

KISIT: gerçek rclone/GDrive ÇALIŞTIRILMAZ — subprocess/motor fonksiyonları mock'lu.
Kullanım: python3 test_memory_fixes.py   (pytest gerekmez, sıfır bağımlılık)
"""
import json
import os
import re
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

TMP = tempfile.mkdtemp(prefix="memfix_test_")
PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name, detail=""):
    print(f"  ✗ FAIL: {name} {detail}")
    sys.exit(1)


def make_cfg():
    return {
        "identity": {"user_id": "cumulusnet", "machine_id": "test-node-1"},
        "gdrive": {"user_root": "gdrive:hermes-sync/cumulusnet"},
        "machine": "H1",
    }


def make_record(rid, ns="shared", revision=1, hlc=None, tombstone=False, **extra):
    rec = {"record_id": rid, "namespace": ns, "subject": f"subj-{rid}",
           "predicate": "note", "value": f"value-{rid}", "value_type": "string",
           "revision": revision, "hlc": hlc or f"1000.0000-node-{rid}",
           "tombstone": tombstone}
    rec.update(extra)
    return rec


# ── 1. Recursive secret tarama (OceanAPI #2) ──────────────────────────────
print("\n── RECURSIVE SECRET SCAN ──")
# nested dict içinde gizlenmiş secret
rec_nested = make_record("r1", metadata={"deep": {"api_key": "sk-abcdefghijklmnopqrstuvwx"}})
res = smem.scan_payload_for_secrets(rec_nested)
if res["ok"]:
    fail("nested secret yakalanmadı", str(res))
ok("nested dict secret RED")

# list içinde gizlenmiş secret
rec_list = make_record("r2", sources=["x", {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"}])
res = smem.scan_payload_for_secrets(rec_list)
if res["ok"]:
    fail("list secret yakalanmadı", str(res))
ok("nested list secret RED")

# ALLOWED_VALUE_FIELDS dışındaki string alanda secret (source)
rec_src = make_record("r3", source={"node": "h2", "note": "Bearer abc12345.def67890.ghi"})
res = smem.scan_payload_for_secrets(rec_src)
if res["ok"]:
    fail("source alanındaki Bearer yakalanmadı", str(res))
ok("source alanında Bearer RED")

# temiz kayıt — false positive yok
rec_clean = make_record("r4", value="make release", metadata={"n": 5})
res = smem.scan_payload_for_secrets(rec_clean)
if not res["ok"]:
    fail("temiz kayıt RED oldu", str(res))
ok("temiz kayıt GEÇTİ")

# ── 2. Import secret savunması (OceanAPI #3) ──────────────────────────────
print("\n── IMPORT SECRET RED ──")
mdir = os.path.join(TMP, "m2")
os.makedirs(os.path.join(mdir, "shared"), exist_ok=True)
delta_bad = os.path.join(TMP, "bad.jsonl")
with open(delta_bad, "w") as f:
    f.write(json.dumps(make_record("sec1", revision=9)) + "\n")
    f.write(json.dumps(make_record("sec2", revision=9, value="sk-abcdefghijklmnopqrstuvwx")) + "\n")
res = smem.import_memory_delta(mdir, delta_bad, "test-node-1")
if res.get("rejected_secret", 0) != 1:
    fail("import secret RED sayısı", str(res))
if os.path.exists(os.path.join(mdir, "shared", "sec2.json")):
    fail("secret kayıt yazıldı (fail-closed ihlali)")
if not os.path.exists(os.path.join(mdir, "shared", "sec1.json")):
    fail("temiz kayıt da reddedildi")
ok(f"import {res['rejected_secret']} secret kayıt RED (temiz kayıt uygulandı)")

# ── 3. Eski tombstone yeni kaydı silmez (OceanAPI #4) ─────────────────────
print("\n── STALE TOMBSTONE ──")
mdir3 = os.path.join(TMP, "m3")
os.makedirs(os.path.join(mdir3, "shared"), exist_ok=True)
# önce yeni kayıt (rev 5)
with open(os.path.join(mdir3, "shared", "alive.json"), "w") as f:
    json.dump(make_record("alive", revision=5, hlc="5000.0000-node-x"), f)
# sonra ESKİ tombstone (rev 2)
delta_t = os.path.join(TMP, "tomb.jsonl")
with open(delta_t, "w") as f:
    f.write(json.dumps(make_record("alive", revision=2, hlc="2000.0000-node-x", tombstone=True)) + "\n")
res = smem.import_memory_delta(mdir3, delta_t, "test-node-1")
if res.get("tombstones", 0) != 0:
    fail("eski tombstone uygulandı — kayıt silindi", str(res))
if not os.path.exists(os.path.join(mdir3, "shared", "alive.json")):
    fail("alive.json kayboldu")
ok("eski tombstone (rev 2 < 5) atlandı — yeni kayıt korundu")

# yeni tombstone (rev 6 > 5) uygulanmalı ve .tombstone kopyası kalmalı
delta_t2 = os.path.join(TMP, "tomb2.jsonl")
with open(delta_t2, "w") as f:
    f.write(json.dumps(make_record("alive", revision=6, hlc="6000.0000-node-x", tombstone=True)) + "\n")
res = smem.import_memory_delta(mdir3, delta_t2, "test-node-1")
if res.get("tombstones", 0) != 1:
    fail("yeni tombstone uygulanmadı", str(res))
if os.path.exists(os.path.join(mdir3, "shared", "alive.json")):
    fail("canonical alive.json silinmedi")
tomb_copies = [f for f in os.listdir(os.path.join(mdir3, "shared"))
               if ".tombstone." in f]
if not tomb_copies:
    fail(".tombstone kopyası yok (veri korunmadı)")
ok("yeni tombstone uygulandı + .tombstone kopyası korundu")

# ── 4. Export dosya adı µs+uuid (OceanAPI #1) ─────────────────────────────
print("\n── EXPORT FİLENAME ──")
mdir4 = os.path.join(TMP, "m4")
for ns in ("shared", "private", "quarantine"):
    os.makedirs(os.path.join(mdir4, ns), exist_ok=True)
with open(os.path.join(mdir4, "shared", "f.json"), "w") as f:
    json.dump(make_record("f", revision=1), f)
e1 = smem.export_memory_delta(mdir4, "test-node-1", "cumulusnet", since_seq=0)
e2 = smem.export_memory_delta(mdir4, "test-node-1", "cumulusnet", since_seq=0)
if e1["delta"] == e2["delta"]:
    fail("iki export aynı dosya adı (overwrite riski)", e1["delta"])
if not os.path.exists(e1["delta"]) or not os.path.exists(e2["delta"]):
    fail("export dosyaları yok")
ok("iki hızlı export farklı dosya (µs+uuid soneki)")

# ── 5. Conflict/tombstone dosya adı µs (#6) ───────────────────────────────
print("\n── CONFLICT FILENAME µs ──")
mdir5 = os.path.join(TMP, "m5")
os.makedirs(os.path.join(mdir5, "shared"), exist_ok=True)
with open(os.path.join(mdir5, "shared", "c.json"), "w") as f:
    json.dump(make_record("c", revision=3, hlc="3000.0000-node-a"), f)
delta_c = os.path.join(TMP, "conf.jsonl")
with open(delta_c, "w") as f:
    f.write(json.dumps(make_record("c", revision=3, hlc="3001.0000-node-b")) + "\n")
res = smem.import_memory_delta(mdir5, delta_c, "test-node-1")
if res.get("conflicts", 0) != 1:
    fail("conflict oluşmadı", str(res))
cfiles = [f for f in os.listdir(os.path.join(mdir5, "shared")) if ".conflict." in f]
if not cfiles:
    fail("conflict dosyası yok")
if not all(re.search(r"T\d{12}Z", f) for f in cfiles):
    fail("conflict dosya adında µs yok", str(cfiles))
ok("conflict dosyası µs adresli korundu")

# ── 6. Audit append kilitli zincir (#7) ───────────────────────────────────
print("\n── AUDIT LOCKED CHAIN ──")
ad = os.path.join(TMP, "audit")
h1 = smem.append_audit_event(ad, {"node_id": "h1", "event_type": "test", "path": "/x"})
h2 = smem.append_audit_event(ad, {"node_id": "h1", "event_type": "test2", "path": "/y"})
v = smem.verify_audit_chain(ad)
if not v.get("ok"):
    fail("audit zinciri kırık", str(v))
if h1 == h2 or len(h1) != 64:
    fail("hash beklenmiyor")
ok(f"audit zinciri bozulmadı (h1={h1[:8]}… h2={h2[:8]}…)")
# helper'lar doğru çalışıyor
if smem._audit_last_hash(ad + "/" + os.listdir(ad)[0]) != h2:
    fail("_audit_last_hash son hash'i döndürmüyor")
ok("_audit_last_hash son hash doğru")

# ── 7. cmd_memory hata yayılımı (OceanAPI #8) ─────────────────────────────
print("\n── CMD_MEMORY HATA YAYILIMI ──")
cfg = make_cfg()
mdir7 = os.path.join(TMP, "m7")
os.makedirs(mdir7, exist_ok=True)

# TÜM durumlarda export+push mock'lu — gerçek rclone/GDrive'a DOKUNULMAZ
orig_export, orig_push = motor.memory_export, motor.memory_push
orig_pull, orig_fs = motor.memory_pull_import, motor.memory_to_fact_store

# pull_import HARD hata (-1) → rc=1
motor.memory_export = lambda *a, **k: os.path.join(TMP, "dummy.jsonl")
motor.memory_push = lambda *a, **k: True
motor.memory_pull_import = lambda *a, **k: -1
try:
    rc = motor.cmd_memory(cfg, dry_run=False, memory_dir=mdir7, memory_db=None)
    if rc != 1:
        fail("pull_import -1 iken rc=1 dönmedi", f"rc={rc}")
    ok("pull_import hard hata → cmd_memory rc=1")
finally:
    pass  # mock'lar diğer durumlar için kalıcı

# fact_store hard hata (-1) → rc=1
motor.memory_pull_import = lambda *a, **k: 0
motor.memory_to_fact_store = lambda *a, **k: -1
try:
    rc = motor.cmd_memory(cfg, dry_run=False, memory_dir=mdir7, memory_db=None)
    if rc != 1:
        fail("fact_store -1 iken rc=1 dönmedi", f"rc={rc}")
    ok("fact_store hard hata → cmd_memory rc=1")
finally:
    pass

# tam başarı → rc=0
motor.memory_pull_import = lambda *a, **k: 3
motor.memory_to_fact_store = lambda *a, **k: 2
try:
    rc = motor.cmd_memory(cfg, dry_run=False, memory_dir=mdir7, memory_db=None)
    if rc != 0:
        fail("başarılı akış rc=0 dönmedi", f"rc={rc}")
    ok("başarılı akış → cmd_memory rc=0")
finally:
    motor.memory_export, motor.memory_push = orig_export, orig_push
    motor.memory_pull_import = orig_pull
    motor.memory_to_fact_store = orig_fs

# ── 8. Tombstone index / resurrection önleme (2. tur #1/#2/#3) ────────────
print("\n── TOMBSTONE INDEX / RESURRECTION ──")

# 8a. tombstone ÖNCE import (hedef yok), sonra eski kayıt → diriltme YOK
mdir8a = os.path.join(TMP, "m8a")
os.makedirs(os.path.join(mdir8a, "shared"), exist_ok=True)
d_tomb = os.path.join(TMP, "t8a_tomb.jsonl")
with open(d_tomb, "w") as f:
    f.write(json.dumps(make_record("res", revision=7, hlc="7000.0000-node-x", tombstone=True)) + "\n")
r1 = smem.import_memory_delta(mdir8a, d_tomb, "test-node-1")
if r1.get("tombstones", 0) != 1:
    fail("tombstone index yazılmadı", str(r1))
if not os.path.exists(os.path.join(mdir8a, "tombstones", "shared", "res.json")):
    fail("tombstones/res.json index yok")
ok("tombstone önce import edildi + index yazıldı")

d_stale = os.path.join(TMP, "t8a_stale.jsonl")
with open(d_stale, "w") as f:
    f.write(json.dumps(make_record("res", revision=3, hlc="3000.0000-node-y")) + "\n")
r2 = smem.import_memory_delta(mdir8a, d_stale, "test-node-1")
if r2.get("resurrected", 0) != 1 or r2.get("applied", 0) != 0:
    fail("eski kayıt diriltildi", str(r2))
if os.path.exists(os.path.join(mdir8a, "shared", "res.json")):
    fail("silinmiş kayıt yeniden oluşturuldu (resurrection)")
ok("eski kayıt (rev 3 < 7) diriltilmedi")

# 8b. daha YENİ kayıt → meşru diriltme, index temizlenir
d_new = os.path.join(TMP, "t8b_new.jsonl")
with open(d_new, "w") as f:
    f.write(json.dumps(make_record("res", revision=9, hlc="9000.0000-node-y")) + "\n")
r3 = smem.import_memory_delta(mdir8a, d_new, "test-node-1")
if r3.get("applied", 0) != 1:
    fail("yeni kayıt uygulanmadı", str(r3))
if not os.path.exists(os.path.join(mdir8a, "shared", "res.json")):
    fail("meşru diriltme yazılmadı")
if os.path.exists(os.path.join(mdir8a, "tombstones", "shared", "res.json")):
    fail("tombstone index temizlenmedi")
ok("yeni kayıt (rev 9 > 7) meşru diriltme + index temizlendi")

# 8c. eşit revision + farklı hlc tombstone → mevcut kayıt KORUNUR (conflict)
mdir8c = os.path.join(TMP, "m8c")
os.makedirs(os.path.join(mdir8c, "shared"), exist_ok=True)
with open(os.path.join(mdir8c, "shared", "eq.json"), "w") as f:
    json.dump(make_record("eq", revision=5, hlc="5000.0000-node-a"), f)
d_eq = os.path.join(TMP, "t8c_eq.jsonl")
with open(d_eq, "w") as f:
    f.write(json.dumps(make_record("eq", revision=5, hlc="5001.0000-node-b", tombstone=True)) + "\n")
r4 = smem.import_memory_delta(mdir8c, d_eq, "test-node-1")
if r4.get("conflicts", 0) != 1 or r4.get("tombstones", 0) != 0:
    fail("eşit rev tombstone conflict sayılmadı", str(r4))
if not os.path.exists(os.path.join(mdir8c, "shared", "eq.json")):
    fail("eşit rev tombstone mevcut kaydı sildi")
ok("eşit rev + farklı hlc tombstone: kayıt korundu (conflict), index yazıldı")

print(f"\n✅ TÜM TESTLER GEÇTİ — {PASS} PASS")
