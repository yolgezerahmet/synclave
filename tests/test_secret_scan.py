#!/usr/bin/env python3
"""Gizli-anahtar TARAMA regresyon kapısı (v2.1.1).

NEDEN VAR
---------
`scan_directory` içindeki CONTENT_PATTERNS bir dönem REGEX gibi yazılmıştı
(b"AKIA[0-9A-Z]{16}", b"xox[baprs]-", b"sk-[A-Za-z0-9]{20,}"). Bu diziler
`bytes` literal'dir ve `p in head` ile ALT DİZE araması yapar — yani köşeli
parantezli desenler GERÇEK anahtarla ASLA eşleşmez. Sonuç: secret taraması
sessizce ölüydü (AWS/Slack/sk- anahtarları manifest'e sızabilirdi).

Bu dosya o hatanın geri gelmesini engeller: her sağlayıcı öneki için gerçekçi
bir anahtar içeren aday dosya taranır ve manifest'e GİRMEMESİ doğrulanır.

KAPSAM NOTU: içerik taraması yalnızca CONTENT_SCAN_NAMES listesindeki riskli
dosya adlarına uygulanır (performans); kod dosyaları taranmaz.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import synclave.sync_motor as sm


def _cfg(tmp_path):
    return {"path": str(tmp_path), "include": ["*"],
            "exclude_dirs": [], "max_size_kb": 512}


def _scan(tmp_path):
    return sm.scan_directory("t", _cfg(tmp_path))


# Gerçekçi anahtar gövdeleri (sahte ama biçim doğru) — sağlayıcı öneki
# CONTENT_PATTERNS'te literal alt dize olarak bulunmalı.
REAL_KEYS = {
    "aws": b"AKIAIOSFODNN7EXAMPLE",                 # AWS access key id
    "slack_bot": b"xoxb-123456789012-abcdefghijkl",  # Slack bot token
    "slack_user": b"xoxp-123456789012-abcdefghijkl",  # Slack user token
    "openai_sk": b"sk-abcdefghijklmnopqrstuvwxyz012345",
    "qcode": b"cr_2690abcdefghijklmnop",
    "yapayzekalab": b"yzk_live_abcdefghijklmnop",
    "nvidia": b"nvapi-AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "firecrawl": b"fc-b005a1a56abcdef0123456789",
    "google": b"AIzaSyA1234567890abcdefghijklmnopqrst",
    "github_pat": b"github_pat_11ABCDEFG0abcdefghijklmnop",
    "github_classic": b"ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "private_key": b"-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq",
    "openssh_key": b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt",
}


@pytest.mark.parametrize("provider,body", sorted(REAL_KEYS.items()))
def test_content_scan_blocks_real_key_in_candidate_file(tmp_path, provider, body):
    """Her sağlayıcının gerçek anahtar biçimi aday dosyada YAKALANMALI."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_bytes(json.dumps({"token": body.decode("latin-1")}).encode())
    inv = _scan(tmp_path)
    assert inv == {}, f"{provider}: secret tarama KAÇIRDI -> {list(inv)}"


@pytest.mark.parametrize("provider,body", sorted(REAL_KEYS.items()))
def test_content_scan_blocks_key_in_yaml_candidate(tmp_path, provider, body):
    """config.yaml da aday listede (8 Eyl eklemesi) — kaçmamalı."""
    (tmp_path / "config.yaml").write_bytes(b"api_key: " + body + b"\n")
    assert _scan(tmp_path) == {}, f"{provider}: config.yaml kaçışı"


def test_clean_candidate_file_is_included(tmp_path):
    """Regresyon: temiz config dosyası ELENMEMELİ (aşırı-blok yok).

    NOT: 'sk-', 'cr_' vb. literal alt dizedir; 'risk-yonetimi' gibi bir değer
    'sk-' içerdiği için fail-closed olarak bloklanır. Bu BİLİNÇLİ bir takastır
    (config elenmesi > anahtar sızması); aşağıdaki metin bu desenleri içermez.
    """
    (tmp_path / "config.json").write_text(
        json.dumps({"node": "h1", "interval_min": 90, "verbose": True}))
    inv = _scan(tmp_path)
    assert "t/config.json" in inv, f"temiz config elendi: {inv}"


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production",
                                  ".env.20260910", "server.key", "id_rsa",
                                  "creds.token"])
def test_env_variants_blocked_by_name(tmp_path, name):
    """`.env.*` düzeltmesi: uzantılı .env varyantları da ada göre elenir."""
    (tmp_path / name).write_text("API_KEY=sk-should-never-sync\n")
    assert _scan(tmp_path) == {}, f"{name} ada göre elenmedi"


def test_non_candidate_file_with_key_like_text_not_blocked(tmp_path):
    """İçerik taraması yalnızca aday adlara uygulanır — kod/not dosyası taranmaz."""
    (tmp_path / "notes.md").write_text("ornek: sk-abcdefghijklmnopqrstuvwxyz\n")
    inv = _scan(tmp_path)
    assert "t/notes.md" in inv, f"aday olmayan dosya beklendiği gibi geçmedi: {inv}"


def test_no_regex_style_dead_patterns_in_source():
    """KAYNAK KAPISI: CONTENT_PATTERNS'te regex-benzeri ölü desen olmamalı.

    Köşeli parantez/süslü parantez içeren bytes desenleri literal aramada
    eşleşmez; bu kapı hatanın kaynağa geri dönmesini engeller.
    """
    src = open(sm.__file__, "r", encoding="utf-8").read()
    start = src.index("CONTENT_PATTERNS = (")
    block = src[start:src.index(")", start)]
    for bad in ("[", "]", "{", "}", "*", "+"):
        assert bad not in block, (
            f"CONTENT_PATTERNS içinde literal aramada eşleşmeyen '{bad}' var "
            f"(regex gibi yazılmış ölü desen) — block: {block!r}")


# ─── İKİ KONUM KAPISI ───────────────────────────────────────────
# Depoda sync_motor iki yerde durur: paket (synclave/sync_motor.py) ve kök
# düz kopya (sync_motor.py). node_agent.py kök kopyayı ÇALIŞTIRIR
# (MOTOR = MOTOR_DIR / "sync_motor.py"), tests/test_sync_motor.py da onu
# import eder. Kopyalardan biri düzeltilmeden kalırsa secret taraması o yolda
# ölü kalır — bu kapı iki konumun aynı desenleri taşımasını zorlar.

def _content_patterns_block(path):
    src = open(path, "r", encoding="utf-8").read()
    start = src.index("CONTENT_PATTERNS = (")
    return src[start:src.index(")", start)]


def test_flat_copy_has_same_secret_patterns():
    """Kök düz kopya, paketle AYNI canlı desenleri taşımalı (node_agent yolu)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flat = os.path.join(repo, "sync_motor.py")
    if not os.path.exists(flat):
        pytest.skip("kök düz kopya yok (paket-only dağıtım)")
    flat_block = _content_patterns_block(flat)
    pkg_block = _content_patterns_block(sm.__file__)
    assert flat_block == pkg_block, (
        "kök kopya ile paket desenleri AYRIŞTI — node_agent eski/taramasız "
        f"desenlerle çalışır.\nflat: {flat_block!r}\npkg : {pkg_block!r}")
    for bad in ("[", "]", "{", "}", "*", "+"):
        assert bad not in flat_block, f"kök kopyada ölü desen: '{bad}'"
