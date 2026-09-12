#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_common_knowledge.py — Ortak Akıl (E modülü, v2.1 — 2026-08-29)
=====================================================================
GPT-5.6 (OceanAPI) tasarımı: GDrive hub üzerinde dağıtık ortak durum +
görev kuyruğu. Rclone üzerinde gerçek distributed lock yok — HLC + son
okuma-çakışma kontrolü yarışı azaltır; kesin atomiklik için ileride
lock/manifest servisi gerekir (not: tasarım sınırı).

Ortak durum dosyası:
  gdrive:hermes-sync/<user>/shared/state.json
  Her makine kendi bloğunu HLC saatli yazar; tüm makineler okur.

Ortak görev kuyruğu:
  gdrive:hermes-sync/<user>/shared/tasks/<task_id>.json
  {task_id, title, status: pending|running|done, owner, hlc, sha, ts}
  Claim: pending → running (yalnız bir makine sahiplenir);
  done: yalnız sahibi yapabilir.

İlkeler: non-destructive, HLC mantıksal saat (duvar saati sıralayıcı değil),
fail-closed (rclone hatası → raise CommonKnowledgeError).
"""
import argparse
import copy
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("sync_common_knowledge")

GDRIVE_HUB = "gdrive:hermes-sync"
DEFAULT_USER = os.environ.get("SYNC_HUB_USER", "hahmet")
SHARED_DIR = "shared"
STATE_PATH = "state.json"
TASKS_DIR = "tasks"

VALID_STATUS = ("pending", "running", "done")

# Kullanıcı adı / task_id path güvenliği (SECURITY — OceanAPI denetim 2026-08-30)
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class CommonKnowledgeError(Exception):
    """Ortak akıl işlem hatası — fail-closed (rclone hata → raise)."""


def _machine_name() -> str:
    try:
        return socket.gethostname().lower().split(".")[0]
    except Exception:
        return "unknown"


def _now_iso() -> str:
    """UTC ISO-8601, Z sonekli (çift offset hatası yok — OceanAPI #12)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_hlc(hlc: Any):
    """HLC string'ini (ms.counter-node) bileşenlerine ayır; bozuk → (0,0,'')."""
    try:
        ms, rest = str(hlc).split(".", 1)
        cnt, node = rest.split("-", 1)
        return int(ms), int(cnt), node
    except Exception:
        return 0, 0, ""


def _hlc_value(hlc: Any = None) -> str:
    """İlk/bağımsız HLC üret — önceki değer varsa İLERLET (OceanAPI #11).

    HLC: <fiziksel_ms>.<counter>-<node>. Fiziksel saat önceki HLC'den
    büyükse sayaç sıfırlanır; aksi halde (saat aynı/geri) sayaç artar —
    monoton artış garantisi, dağıtık sıralama çakışmaları azalır.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    node = _machine_name()
    if hlc is None:
        return f"{now_ms}.0000-{node}"
    prev_ms, prev_cnt, prev_node = _parse_hlc(hlc)
    node = prev_node or node
    if now_ms > prev_ms:
        return f"{now_ms}.0000-{node}"
    return f"{max(prev_ms, now_ms)}.{prev_cnt + 1:04d}-{node}"


def _is_not_found(rc: int, err: str) -> bool:
    """rclone 'dosya yok' durumunu gerçek hatadan ayır (OceanAPI #8/#9)."""
    if rc == 0:
        return False
    e = (err or "").lower()
    return "not found" in e or "yok" in e or "doesn't exist" in e


def _hub_base(user: Optional[str] = None) -> str:
    u = user or DEFAULT_USER
    if not _USER_RE.match(u):
        raise CommonKnowledgeError(f"geçersiz kullanıcı: {u!r} (^[A-Za-z0-9._-]{{1,64}}$)")
    return f"{GDRIVE_HUB}/{u}"


# ─── rclone hata dayanıklılığı (v2.1.1 — 30 Ağu 2026) ───────────
# Geçici ağ/5xx hatalarında YALNIZCA idempotent OKUMA komutlarına
# 1 retry (3s bekle) uygulanır; yazma komutlarına (copy/copyto)
# ASLA retry YOK — çift yazma/kısmi durum riski fail-closed korunur.
# KANONİK okuma kümesi — sync_motor._RETRY_READ_TOKENS ile BİREBİR aynı
# olmak zorundadır: aynı güvenlik politikasının iki uygulaması vardır ve
# ayrışırlarsa dayanıklılık sessizce zayıflar. Sapma
# tests/test_retry.py::test_okuma_kumeleri_kume_olarak_esit ile kapılır.
# Hepsi yan etkisiz SORGUDUR (dosya okuma, dizin listeleme, iş durumu,
# bağlantı/kuota kontrolü) — retry güvenlidir. Yazma komutları bu kümeye
# ASLA girmez (çift yazma/kısmi durum riski fail-closed korunur).
_RCLONE_READ_COMMANDS = {"cat", "lsf", "lsjson", "lsd", "status", "ping",
                         "listremotes", "direxists", "about"}
_HTTP_5XX_RE = re.compile(r"\b5\d\d\b")
_NET_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "temporary failure",
    "tls handshake timeout",
    "i/o timeout",
    "service unavailable",
    "timeout",
)
# rc==-1 (exception) durumunda KALICI hatalar geçici sayılmaz
# (rclone executable yok, dosya yok, izin reddi — retry anlamsız).
_FATAL_RC_MINUS1 = ("no such file", "not found", "command not found",
                    "permission denied", "is a directory")


def _rclone_command_text(args: List[str]) -> str:
    try:
        return shlex.join(["rclone", *map(str, args)])
    except Exception:
        return "rclone " + " ".join(map(str, args))


def _is_rclone_read(args: List[str]) -> bool:
    """Yalnızca idempotent okuma komutları retry edilebilir."""
    return bool(args) and str(args[0]).lower() in _RCLONE_READ_COMMANDS


def _is_transient_rclone_error(rc: int, stderr: str) -> bool:
    """Geçici hata mı? (timeout, network, HTTP 5xx). 'not found' HAYIR."""
    text = (stderr or "").lower()
    if _is_not_found(rc, text):
        return False
    if rc == -1:          # TimeoutExpired / exception
        return not any(m in text for m in _FATAL_RC_MINUS1)
    if _HTTP_5XX_RE.search(text):
        return True
    return any(m in text for m in _NET_MARKERS)


def _run_rclone(args: List[str], timeout: int = 180):
    """rclone çalıştır → (rc, stdout, stderr).

    Okuma komutlarında geçici hata (timeout/network/5xx) → 1 retry (3s).
    Yazma komutlarına retry YOK. Hata durumunda stderr'e
    'sync hata: <komut> rc=<rc> <süre>s retry=<n>' tanısı öneklenir
    (fail-closed korunur — çağrıcı rc!=0 görür ve raise eder).
    """
    command = _rclone_command_text(args)
    can_retry = _is_rclone_read(args)
    retries = 1 if can_retry else 0
    rc, out, err = -1, "", ""
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            r = subprocess.run(["rclone", *args], capture_output=True, text=True,
                               errors="replace", timeout=timeout)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = -1, "", f"TIMEOUT {timeout}s"
        except Exception as e:
            rc, out, err = -1, "", str(e)
        dur = time.monotonic() - t0
        if rc == 0:
            return rc, out, err
        if attempt < retries and _is_transient_rclone_error(rc, err):
            log.warning(f"sync hata: {command} rc={rc} {dur:.1f}s retry={attempt + 1}/{retries}")
            time.sleep(3)
            continue
        if rc != 0 and not _is_not_found(rc, err):
            log.error(f"sync hata: {command} rc={rc} {dur:.1f}s retry={attempt}")
        # fail-closed: çağrıcıya tanı önekli hata (yok-ayrımı korunur)
        return rc, out, f"sync hata: {command} rc={rc} {dur:.1f}s retry={attempt} | {err}"


# ─── Ortak durum (state.json) ─────────────────────────────────
def read_state(user: Optional[str] = None) -> Dict[str, Any]:
    """state.json'u oku — yoksa boş sözlük, GERÇEK hata → raise (fail-closed)."""
    rc, out, err = _run_rclone(["cat", f"{_hub_base(user)}/{SHARED_DIR}/{STATE_PATH}"])
    if rc == 0:
        try:
            data = json.loads(out)
            return data if isinstance(data, dict) else {}
        except Exception:
            raise CommonKnowledgeError(f"state.json bozuk JSON: {err.strip()[:150]}")
    if _is_not_found(rc, err):
        return {}
    raise CommonKnowledgeError(f"state.json okunamadı: {err.strip()[:150]}")


def update_state(node: str, updates: Dict[str, Any],
                 user: Optional[str] = None) -> Dict[str, Any]:
    """Kendi durum bloğunu HLC saatli state.json'a yaz (merge).

    Yarış azaltma: yazmadan hemen önce yeniden okuyup diğer makinelerin
    yeni bloklarını korur (OceanAPI #1). Kesin atomiklik yok — rclone
    üzerinde distributed lock ileride.
    """
    state = read_state(user)
    blocks = state.get("nodes", {})
    block = blocks.get(node, {})
    prev_hlc = block.get("hlc")
    block.update(updates)
    block["ts"] = _now_iso()
    block["hlc"] = _hlc_value(prev_hlc)
    blocks[node] = block
    state["nodes"] = blocks
    state["updated_at"] = _now_iso()
    # yazmadan önce son-okuma: araya giren blokları koru (diğer makineler)
    latest = read_state(user)
    if latest:
        for n, b in (latest.get("nodes") or {}).items():
            if n not in state["nodes"]:
                state["nodes"][n] = b
        latest_ua = latest.get("updated_at")
        if latest_ua and latest_ua > state["updated_at"]:
            state["updated_at"] = latest_ua
    _write_state(state, user)
    return state


def _write_state(state: Dict[str, Any], user: Optional[str] = None):
    remote = f"{_hub_base(user)}/{SHARED_DIR}/{STATE_PATH}"
    tmp = tempfile.mkdtemp(prefix="synck_")
    try:
        lp = os.path.join(tmp, STATE_PATH)
        with open(lp, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        rc, _, err = _run_rclone(["copyto", lp, remote], timeout=180)
        if rc != 0:
            raise CommonKnowledgeError(f"state.json yazılamadı: {err.strip()[:150]}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ─── Ortak görev kuyruğu ──────────────────────────────────────
def _task_remote_path(task_id: str, user: Optional[str] = None) -> str:
    """Görev uzak yolu — task_id path güvenli (SECURITY — OceanAPI #6)."""
    if not _valid_task_id(task_id):
        raise CommonKnowledgeError(f"geçersiz task_id: {task_id!r}")
    return f"{_hub_base(user)}/{SHARED_DIR}/{TASKS_DIR}/{task_id}.json"


def _read_remote_json(remote_path: str, default=None, timeout: int = 180):
    """Uzak JSON oku — 'yok' default döner, GERÇEK hata → raise (fail-closed)."""
    rc, out, err = _run_rclone(["cat", remote_path], timeout=timeout)
    if rc == 0:
        try:
            return json.loads(out)
        except Exception:
            raise CommonKnowledgeError(f"bozuk JSON: {remote_path}")
    if _is_not_found(rc, err):
        return default
    raise CommonKnowledgeError(f"okunamadı: {err.strip()[:150]}")


def _write_remote_json(remote_path: str, data: Dict[str, Any]):
    tmp = tempfile.mkdtemp(prefix="synct_")
    try:
        lp = os.path.join(tmp, os.path.basename(remote_path))
        with open(lp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        rc, _, err = _run_rclone(["copyto", lp, remote_path], timeout=180)
        if rc != 0:
            raise CommonKnowledgeError(f"yazılamadı: {err.strip()[:150]}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def create_task(task_id: str, title: str, owner: Optional[str] = None,
                hlc: Any = None, user: Optional[str] = None,
                sha: str = "") -> Dict[str, Any]:
    """Görev oluştur — aynı id varsa RED (üzerine yazmaz)."""
    if not task_id or not _valid_task_id(task_id):
        raise CommonKnowledgeError(f"geçersiz task_id: {task_id!r}")
    remote_path = _task_remote_path(task_id, user)
    existing = _read_remote_json(remote_path, None)
    if isinstance(existing, dict):
        raise CommonKnowledgeError(f"Task zaten var: {task_id}")
    task = {
        "task_id": task_id, "title": title, "status": "pending",
        "owner": owner or "", "hlc": _hlc_value(hlc),
        "sha": sha, "ts": _now_iso(),
        "attempt": 0, "max_attempts": 3,
    }
    _write_remote_json(remote_path, task)
    return task


def claim_task(task_id: str, owner: Optional[str] = None,
               hlc: Any = None, user: Optional[str] = None,
               allow_stale: bool = False, stale_minutes: int = 30,
               force_stale: bool = False) -> Dict[str, Any]:
    """pending görevi sahiplen → running (yalnız bir makine).

    FAILOVER (v2.2, OceanAPI tasarımı): allow_stale=True iken running
    görevin sahibi state.json'da yoksa VEYA görev stale_minutes'dan
    eskiyse başka node devralabilir (attempt++). Terminal state (done)
    ASLA geri alınamaz; max_attempts aşımı → failed (loop yok).
    """
    owner = owner or _machine_name()
    remote_path = _task_remote_path(task_id, user)
    task = _read_remote_json(remote_path, None)
    if not isinstance(task, dict):
        raise CommonKnowledgeError(f"Task bulunamadı: {task_id}")
    if task.get("status") in ("done", "failed"):
        return task  # terminal state korunur
    if task.get("status") == "running":
        if not allow_stale:
            return task
        owner_m = task.get("owner") or ""
        stale = force_stale
        if not stale:
            try:
                state = read_state(user)
                alive = owner_m in state.get("nodes", {})
                age_min = 10 ** 9
                ts = task.get("ts", "")
                if ts:
                    from datetime import datetime, timezone
                    try:
                        t0 = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        age_min = (datetime.now(timezone.utc) - t0).total_seconds() / 60.0
                    except Exception:
                        pass
                stale = (not alive) or (age_min > stale_minutes)
            except Exception:
                stale = False
        if not stale:
            return task
        claimed = copy.deepcopy(task)
        claimed["status"] = "running"
        claimed["owner"] = owner
        claimed["attempt"] = int(task.get("attempt", 0)) + 1
        claimed["failover"] = True
        claimed["failover_from"] = owner_m
        claimed["hlc"] = _hlc_value(task.get("hlc"))
        if int(claimed["attempt"]) >= int(task.get("max_attempts", 3)):
            claimed["status"] = "failed"
            claimed["reason"] = "max_attempts aşıldı"
            _write_remote_json(remote_path, claimed)
            return claimed
        _write_remote_json(remote_path, claimed)
        return claimed
    claimed = copy.deepcopy(task)
    claimed["status"] = "running"
    claimed["owner"] = owner
    claimed["hlc"] = _hlc_value(task.get("hlc"))
    # claim öncesi son okuma (yarış azaltma)
    latest = _read_remote_json(remote_path, None)
    if not isinstance(latest, dict) or latest.get("status") != "pending":
        return latest if isinstance(latest, dict) else task
    _write_remote_json(remote_path, claimed)
    return claimed


def done_task(task_id: str, owner: Optional[str] = None,
              hlc: Any = None, user: Optional[str] = None) -> Dict[str, Any]:
    """running görevi yalnız sahibi done yapabilir (pending → done YASAK)."""
    owner = owner or _machine_name()
    remote_path = _task_remote_path(task_id, user)
    task = _read_remote_json(remote_path, None)
    if not isinstance(task, dict):
        raise CommonKnowledgeError(f"Task bulunamadı: {task_id}")
    current_owner = task.get("owner")
    if current_owner and current_owner != owner:
        raise CommonKnowledgeError(
            f"Task başka node tarafından sahiplenilmiş: {current_owner}")
    status = task.get("status")
    if status == "done":
        return task  # idempotent
    if status != "running":
        raise CommonKnowledgeError(
            f"Task 'running' değil ({status}) — önce claim gerekir")
    result = copy.deepcopy(task)
    result["status"] = "done"
    result["owner"] = owner
    result["hlc"] = _hlc_value(task.get("hlc"))
    result["ts"] = _now_iso()
    _write_remote_json(remote_path, result)
    return result


def list_tasks(user: Optional[str] = None) -> List[Dict[str, Any]]:
    """Tüm görevleri oku (pending/running/done)."""
    tasks_dir = f"{_hub_base(user)}/{SHARED_DIR}/{TASKS_DIR}"
    rc, out, err = _run_rclone(["lsf", tasks_dir, "--files-only"])
    if rc != 0:
        return []
    result = []
    for fn in (out or "").splitlines():
        # basename + task_id doğrulama (SECURITY — OceanAPI #7)
        if not fn.endswith(".json") or os.path.basename(fn) != fn:
            continue
        tid = fn[:-5]
        if not _valid_task_id(tid):
            continue
        task = _read_remote_json(f"{tasks_dir}/{fn}", None, timeout=30)
        if isinstance(task, dict):
            result.append(task)
    return sorted(result, key=lambda t: t.get("ts", ""))


def _valid_task_id(task_id: str) -> bool:
    return bool(_TASK_ID_RE.match(task_id)) and len(task_id) <= 64


# ─── CLI ──────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(prog="sync_common_knowledge",
                                 description="Ortak akıl (E) — state + task")
    ap.add_argument("komut", choices=["state", "tasks", "task-add",
                                      "task-claim", "task-done"])
    ap.add_argument("--node", default=None, help="state: makine adı")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--owner", default=None)
    ap.add_argument("--sha", default="")
    ap.add_argument("--user", default=None)
    args = ap.parse_args(argv)

    try:
        if args.komut == "state":
            state = read_state(args.user)
            node = args.node or _machine_name()
            if args.json:
                print(json.dumps(state, ensure_ascii=False, indent=1))
            else:
                blocks = state.get("nodes", {})
                print(f"\n  🧠 ORTAK DURUM — {len(blocks)} makine")
                for n, b in blocks.items():
                    print(f"    {n}: hlc={b.get('hlc','-')} ts={b.get('ts','-')}")
                    for k, v in b.items():
                        if k not in ("hlc", "ts"):
                            print(f"      {k}: {v}")
            return 0
        if args.komut == "tasks":
            tasks = list_tasks(args.user)
            if args.json:
                print(json.dumps(tasks, ensure_ascii=False, indent=1))
            else:
                print(f"\n  📋 ORTAK GÖREVLER — {len(tasks)}")
                for t in tasks:
                    print(f"    [{t.get('status','?'):7}] {t.get('task_id')} "
                          f"— {t.get('title','')} (owner={t.get('owner','-')})")
            return 0
        if args.komut == "task-add":
            if not args.task_id or not args.title:
                print("Kullanım: task-add --task-id <id> --title '<başlık>'")
                return 1
            task = create_task(args.task_id, args.title, owner=args.owner,
                               user=args.user, sha=args.sha)
            if args.json:
                print(json.dumps(task, ensure_ascii=False))
            else:
                print(f"  ➕ task: {task['task_id']} ({task['status']})")
            return 0
        if args.komut == "task-claim":
            if not args.task_id:
                print("Kullanım: task-claim --task-id <id> [--owner <makine>]")
                return 1
            task = claim_task(args.task_id, owner=args.owner, user=args.user)
            if args.json:
                print(json.dumps(task, ensure_ascii=False))
            else:
                print(f"  🔒 task: {task['task_id']} → {task['status']} "
                      f"(owner={task.get('owner','-')})")
            return 0
        if args.komut == "task-done":
            if not args.task_id:
                print("Kullanım: task-done --task-id <id> [--owner <makine>]")
                return 1
            task = done_task(args.task_id, owner=args.owner, user=args.user)
            if args.json:
                print(json.dumps(task, ensure_ascii=False))
            else:
                print(f"  ✅ task: {task['task_id']} → {task['status']}")
            return 0
    except CommonKnowledgeError as e:
        print(f"  ⛔ {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
