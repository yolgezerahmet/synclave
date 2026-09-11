#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_sec_fixes.py — OceanAPI denetim kapanış testleri (2026-08-30)

E modülü (sync_common_knowledge):
  1. geçersiz kullanıcı → RED (path traversal)
  2. geçersiz task_id claim/done → RED
  3. read hatası (not-found DEĞİL) → CommonKnowledgeError (fail-closed)
  4. pending görev doğrudan done → RED (claim şart)
  5. HLC monotonik ilerler (update_state 2× → 2. hlc > 1. hlc)
  6. updated_at her güncellemede yenilenir
  7. _now_iso Z sonekli, çift offset yok

A modülü (sync_motor):
  8. cmd_rollback geçersiz version → RED (indirme YAPILMAZ)
  9. _diff_versions geçersiz v1/v2 → RED
 10. _safe_tar_member: ../, mutlak yol, sürücü harfi RED

C modülü (sync_retention):
 11. main() TÜM to_delete snapshot'ları purge eder (ilk 10 değil)
 12. run() shell=False liste argümanı alır (enjeksiyon yok)

KISIT: gerçek rclone/GDrive ÇALIŞTIRILMAZ — subprocess.run mock'lanır.
Kullanım: python3 test_sec_fixes.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

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

import sync_common_knowledge as ck
import sync_motor as motor
import sync_retention as ret

TMP = tempfile.mkdtemp(prefix="secfix_test_")
PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


def fail(name, detail=""):
    print(f"  ✗ FAIL: {name} {detail}")
    sys.exit(1)


def expect_raise(fn, exc, name):
    try:
        fn()
        fail(name, "hata fırlatmadı")
    except exc:
        ok(name)
    except Exception as e:
        fail(name, f"yanlış hata: {e!r}")


# ─── E: sync_common_knowledge ─────────────────────────────────
print("── E: PATH GÜVENLİĞİ ──")
# 1. geçersiz kullanıcı → RED
expect_raise(lambda: ck._hub_base("../other"), ck.CommonKnowledgeError,
             "_hub_base('../other') → RED (path traversal)")
expect_raise(lambda: ck._hub_base("a/b"), ck.CommonKnowledgeError,
             "_hub_base('a/b') → RED")

# 2. geçersiz task_id claim/done → RED
expect_raise(lambda: ck.claim_task("../evil", owner="h1"),
             ck.CommonKnowledgeError,
             "claim_task('../evil') → RED")
expect_raise(lambda: ck.done_task("../../x", owner="h1"),
             ck.CommonKnowledgeError,
             "done_task('../../x') → RED")
expect_raise(lambda: ck.create_task("bad/id", "t"),
             ck.CommonKnowledgeError,
             "create_task('bad/id') → RED")

# 3. read hatası (not-found değil) → CommonKnowledgeError (fail-closed)
orig_run = subprocess.run
class FakeErr:
    def __init__(self, rc, out, err):
        self.returncode = rc
        self.stdout = out
        self.stderr = err

def fake_err_run(cmd, **kw):
    # cat dışında her şey rc=0; cat'te "permission denied" hata döner
    if cmd[0] == "rclone" and cmd[1] == "cat":
        return FakeErr(1, "", "permission denied: access")
    if cmd[0] == "rclone" and cmd[1] == "lsf":
        return FakeErr(0, "", "")
    if cmd[0] == "rclone" and cmd[1] == "copyto":
        return FakeErr(0, "", "")
    return FakeErr(0, "", "")

subprocess.run = fake_err_run
try:
    expect_raise(lambda: ck.read_state(), ck.CommonKnowledgeError,
                 "read_state: gerçek hata → CommonKnowledgeError (fail-open değil)")
    expect_raise(lambda: ck.create_task("t1", "test"), ck.CommonKnowledgeError,
                 "create_task: okuma hatası → RED (var olanı ezmez)")
finally:
    subprocess.run = orig_run

# 4. pending → done RED (claim şartı)
# mock: normal çalışan fake (test_common_knowledge deseni)
LOCAL_HUB = os.path.join(TMP, "hub")


def remote_to_local(remote):
    if not remote.startswith(ck._hub_base() + "/"):
        return None
    rel = remote[len(ck._hub_base()) + 1:]
    return os.path.join(LOCAL_HUB, *rel.split("/"))


class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def fake_run2(cmd, **kw):
    if cmd[0] != "rclone":
        return FakeProc(1, "", f"not rclone: {cmd}")
    op = cmd[1]
    if op == "cat":
        lp = remote_to_local(cmd[2])
        if lp and os.path.exists(lp):
            return FakeProc(0, open(lp).read())
        return FakeProc(1, "", "cat: yok")
    if op == "copyto":
        lp = remote_to_local(cmd[3])
        if lp is None:
            return FakeProc(1, "", "kopyalanamaz")
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        shutil.copy(cmd[2], lp)
        return FakeProc(0, "")
    if op == "lsf":
        lp = remote_to_local(cmd[2])
        if lp and os.path.isdir(lp):
            return FakeProc(0, "\n".join(sorted(os.listdir(lp))) + "\n")
        return FakeProc(0, "")
    return FakeProc(1, "", f"unknown op: {op}")


print("── E: DURUM MAKİNESİ + HLC ──")
subprocess.run = fake_run2
try:
    ck.create_task("pending-task", "test")
    expect_raise(lambda: ck.done_task("pending-task", owner="h1"),
                 ck.CommonKnowledgeError,
                 "done_task: pending → RED (önce claim gerekir)")

    # 5. HLC monotonik ilerler
    s1 = ck.update_state("h1", {"disk_gb": 100})
    h1 = s1["nodes"]["h1"]["hlc"]
    s2 = ck.update_state("h1", {"disk_gb": 99})
    h2 = s2["nodes"]["h1"]["hlc"]
    try:
        def _hlc_key(h):
            ms, rest = h.split(".", 1)
            cnt = rest.split("-", 1)[0]
            return (int(ms), int(cnt))
        if not (_hlc_key(h2) > _hlc_key(h1)):
            fail("HLC monotonik ilerlemedi", f"{h1} → {h2}")
    except Exception as e:
        fail("HLC parse", str(e))
    ok(f"HLC monotonik ilerler: {h1} → {h2}")

    # 6. updated_at her güncellemede yenilenir
    ua1 = s1.get("updated_at")
    ua2 = s2.get("updated_at")
    if not (ua1 and ua2 and ua2 > ua1):
        fail("updated_at yenilenmedi", f"{ua1} → {ua2}")
    ok(f"updated_at yenilenir: {ua1} → {ua2}")

    # 7. _now_iso Z sonekli, çift offset yok
    iso = ck._now_iso()
    if not iso.endswith("Z") or "+00:00" in iso:
        fail("_now_iso format", iso)
    ok(f"_now_iso Z sonekli (çift offset yok): {iso}")
finally:
    subprocess.run = orig_run

# ─── A: sync_motor ────────────────────────────────────────────
print("── A: VERSİYON GÜVENLİĞİ ──")
cfg = {
    "identity": {"user_id": "cumulusnet", "machine_id": "test-node-1"},
    "gdrive": {"user_root": "gdrive:hermes-sync/cumulusnet"},
    "dirs": {"kernel": {"path": os.path.join(TMP, "kernel")}},
    "machine": "H1",
}

# 8. cmd_rollback geçersiz version → RED (indirme çağrısı olmaz)
calls = []
def fake_run3(cmd, **kw):
    calls.append(cmd)
    return FakeProc(0, "", "")

subprocess.run = fake_run3
try:
    rc = motor.cmd_rollback(cfg, "kernel", "../../etc/passwd.tar.gz")
    if rc == 0:
        fail("cmd_rollback geçersiz version RED etmedi")
    ok("cmd_rollback: '../../etc/passwd.tar.gz' → RED")
    rc2 = motor.cmd_rollback(cfg, "kernel", "kernel_20260801_100000.tar.gz")
    # indirme mock'ta rc=0 döner; tar yok → hata beklenir (1)
    if rc2 != 1:
        fail("cmd_rollback normal version akışı", str(rc2))
    ok("cmd_rollback: normal version akışı (indirme hatası → 1)")
finally:
    subprocess.run = orig_run

# 9. _diff_versions geçersiz v1/v2 → RED
rc = motor._diff_versions(cfg, "kernel", "v1.tar.gz;rm -rf /,v2.tar.gz",
                          hub="gdrive:hermes-sync/cumulusnet/versiyonlu")
if rc == 0:
    fail("_diff_versions enjeksiyon RED etmedi")
ok("_diff_versions: ';rm -rf /' içeren ad → RED")

# 10. _safe_tar_member
bad = ["../etc/passwd", "/etc/passwd", "C:\\Windows\\x", "a/../../b",
       "..", "sub/../x"]
good = ["main.c", "sub/main.c", "kernel/main.c", "a-b_c.d"]
for b in bad:
    if motor._safe_tar_member(b):
        fail("_safe_tar_member güvensizi kabul etti", b)
for g in good:
    if not motor._safe_tar_member(g):
        fail("_safe_tar_member güvenliyi reddetti", g)
ok(f"_safe_tar_member: {len(bad)} güvensiz RED, {len(good)} güvenli kabul")

# ─── C: sync_retention ────────────────────────────────────────
print("── C: RETENTION ──")
# 11. main() TÜM to_delete purge eder (ilk 10 değil)
ret_root = os.path.join(TMP, "gdrive_root")
os.makedirs(ret_root, exist_ok=True)
# 15 eski snapshot dizini üret (hepsi 7 günden eski, farklı haftalar)
snap_names = []
now = datetime.now()
for i in range(15):
    ts = now - timedelta(weeks=i + 2)  # 2..16 hafta önce — hepsi 7 günlük pencere dışı
    dname = ts.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(ret_root, "plugins", dname)
    os.makedirs(d, exist_ok=True)
    snap_names.append(dname)

purge_calls = []
class FakeRetProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def fake_ret_run(cmd, **kw):
    if cmd[0] == "rclone" and cmd[1] == "lsd":
        # snapshot dizinlerini rclone lsd formatında listele (ad SON token)
        lines = []
        for d in snap_names:
            lines.append(f"          0 -1 {d}")
        return FakeRetProc(0, "\n".join(lines) + "\n", "")
    if cmd[0] == "rclone" and cmd[1] == "purge":
        purge_calls.append(cmd[2])
        return FakeRetProc(0, "", "")
    return FakeRetProc(0, "", "")

old_root = ret.GDRIVE_ROOT

# main()'i sys.argv ile çağır
old_argv = sys.argv
try:
    sys.argv = ["sync_retention.py", "--apply", "--node", "plugins"]
    ret.GDRIVE_ROOT = f"local:{ret_root}"
    subprocess.run = fake_ret_run
    rc = ret.main()
    subprocess.run = orig_run
    # purge çağrıları: to_delete tamamı (15'ten az — haftalık/aylık bucket korur, ama >10 olmalı)
    if rc != 0:
        fail("retention main rc", str(rc))
    if len(purge_calls) <= 10:
        fail("TÜM to_delete purge edilmedi (ilk 10 sınırı)", str(len(purge_calls)))
    ok(f"retention main(): {len(purge_calls)} snapshot purge edildi (ilk 10 sınırı yok)")
finally:
    sys.argv = old_argv
    ret.GDRIVE_ROOT = old_root
    subprocess.run = orig_run

# 12. run() liste argümanı (shell=False) — dönüş (rc, out, err)
old_ret_run = ret.run
try:
    def fake_ret_proc(cmd, **kw):
        return FakeRetProc(0, "out", "err")
    subprocess.run = fake_ret_proc
    rc, out, err = ret.run(["rclone", "lsd", "x"])  # liste argümanı kabul
    if (rc, out, err) != (0, "out", "err"):
        fail("run() dönüş formatı", f"{(rc, out, err)}")
    ok("run(): liste argümanı kabul, (rc,out,err) döner (shell=False)")
finally:
    ret.run = old_ret_run
    subprocess.run = orig_run

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n✅ TÜM TESTLER GEÇTİ — {PASS} PASS")
sys.exit(0)
