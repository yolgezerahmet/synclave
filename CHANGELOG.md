# CHANGELOG — Synclave (eski ad: hermes-sync)

## [2.3.2] — 2026-09-10 (rclone OKUMA retry kapsamı tamamlandı)

- **Kapatan modül (GÖREV 1 artığı):** `sync_motor.py` içinde `subprocess.run`
  ile doğrudan yapılan 5 rclone **okuma** çağrısı (`lsjson --hash`,
  `lsf` ×3, ortak-hafıza `lsf`) `run_cmd`'in retry politikasını atlıyordu;
  `cmd_memory` okuması ayrıca 90s timeout ile spec'in (180s) dışındaydı.
- **Yeni yardımcı `rclone_read(args, timeout=180)`:** politika tekrarı YOK —
  `_is_idempotent_read()` + `_is_transient_rc()` yeniden kullanılır; geçici
  hata (timeout/network/HTTP 5xx) → 1 retry (3s). Log biçimi `run_cmd` ile
  aynı: `sync hata: <komut> rc=<rc> <süre>s retry=<n>`. Dönüş `(rc, out, err)`
  — çağırıcıların stderr tabanlı kullanıcı mesajı korunur, "uzakta yok"
  ayrımı bozulmaz (fail-closed).
- **Yazma güvenliği:** `rclone_read` yazma alt-komutu (copy/copyto/move/sync/
  delete/…) ile çağrılırsa `ValueError` — çift yazma/kısmi durum engellenir.
  Yazma çağrıları (`copyto`/`copy`) doğrudan `subprocess.run` ile kalır; onlara
  retry EKLENMEZ.
- **Regresyon kapısı (`tests/test_retry.py`, +8 test):** kaynak taramasıyla
  `subprocess.run(["rclone", "<verb>"])` içinde yalnız YAZMA verb'lerine izin
  verilir (okuma geri sızarsa test kırmızı); retry/başarı, kalıcı hatada retry
  yok, timeout→retry, yazma reddi ve 180s varsayılanı ayrıca kilitlenir.
- Test: 205 PASS (`python3 -m pytest tests/`).

## [1.0.0] — 2026-08-30 (REBRAND: hermes-sync → Synclave)

- Yeni isim: **Synclave** (sync + enclave — sifreli guvenli bolge)
- PyPI: `pip install synclave` - CLI: `synclave`, `synclave-a2a`, `synclave-worker`
- Modul: `synclave/` (eski `hermes_sync/`)
- Icerik: v2.3.1'in birebir aynisi + OceanAPI guvenlik denetim fix'leri
- Eski paket `hermes-sync` PyPI'da deprecated olarak durur


## [2.1.1] — 2026-08-30

### Eklenen — HATA DAYANIKLILIĞI + WINDOWS UYUM
- `sync_common_knowledge._run_rclone`: timeout 120→180s; idempotent OKUMA
  komutlarında (cat/lsf/lsjson/lsd) geçici hata (timeout/network/HTTP 5xx)
  → 1 retry (3s bekle). Yazma (copy/copyto) ASLA retry — fail-closed korunur.
- `sync_motor.run_cmd`: opsiyonel `retries` parametresi — yalnızca idempotent
  okuma (cat/lsf/status) + geçici hatada 1 retry; yazma komutlarına retry YOK.
- Hata logu güçlendirildi: `sync hata: <komut> rc=<rc> <süre>s retry=<n>`
  (süre ölçümü `time.monotonic`); `_run_rclone` hatada stderr'e tanı öneki.
- rclone doğrudan çağrılarının timeout'ları 120→180s.
- Windows uyum: `_motor_lock_path()` — kilit `%TEMP%\cumulus_sync.lock`
  (Windows) / `/tmp/cumulus_sync.lock` (POSIX); path'ler `os.path.join` ile.
- A2A server: `uvicorn` yoksa net hata mesajı + exit 1 (ham traceback değil).
- Testler: `tests/test_retry.py` (12) + `tests/test_windows_uyum.py` (8) —
  toplam 109 PASS. README'ye Windows Kurulum bölümü eklendi.

## [1.6.0] — 2026-08-13

### Eklenen — AKILLI KURULUM (Kaynak Farkındalıklı Öneri)
- `probe` komutu: yerel CPU/RAM/disk/GPU kaynaklarını ölçer (nvidia-smi →
  lspci → vulkaninfo), tools kataloğunu tarar, manifest'e `resources` +
  `tools_state` yazar → push ile karşı node'a gider
- `propose` komutu: karşı node'da kurulu araçları KAYNAK KONTROLLÜ öneri
  listesine çevirir. GPU öncelikli sıralama; NVIDIA GPU'suz makinede CUDA
  zorunlu araçlar engelliye düşer; disk/RAM/CPU eşikleri denetlenir
  (DISK_INSUFFICIENT / RAM_INSUFFICIENT / CPU_INSUFFICIENT / GPU_MISSING)
- `apply --tool <ad> [--yes]` komutu: onay sonrası kurulum. Non-destructive
  garantileri: zaten kuruluysa RED (üzerine asla yazma), kaynak yetersizse
  RED, `--yes` yoksa interaktif onay (reddedilirse HİÇBİR ŞEY çalışmaz)
- Config `tools` kataloğu: check/gpu/min_ram_gb/min_disk_gb/min_cpus/install
  alanları (cuda-toolkit, vllm, ollama, docker, zephyr-sdk, kicad-cli,
  arm-none-eabi-gcc, qemu-system-arm, ns3)
- `push` artık kaynak + araç durumunu manifest'e otomatik ekler (eşitleme
  sırasında akıllılık; kurulum asla otomatik değildir)

### Düzeltilen
- GPU tespiti iki katmanlı: genel GPU (lspci/vulkan) ayrı, NVIDIA/CUDA
  (nvidia-smi) ayrı — virtio/VGA gibi CUDA uyumsuz GPU'lar CUDA araçlarını
  önerilmez yapar (fail-closed)

## [1.5.0] — 2026-08-12

### Eklenen — Hermes Agent Eşitleri
- `hermes-sessions` node: bir ajanın oturum bilgisi (PROJECT_STATE + kapanış
  özeti, `scripts/hermes_session_digest.py`) eşlere paylaşılır — diğer ajan
  çekip öğrenir
- `hermes-profile` node: config + cron + plugin manifest (SECRETS hariç)
- `validate_skills.py`: skill aktarım kapısı — SKILL.md varlığı + frontmatter +
  references bütünlüğü (kicad skill'inde 4 kırık referans yakaladı)

## [2.2.0] — 2026-08-30 (Ajan Kimliği + Sohbet Etiketleme)

### Yeni — Kopyalanamaz-kanıtlı ajan kimliği (agent_identity.py)
- **Ed25519 kimlik**: `agent_id` = açık anahtar özeti → `hx-...` (Hermes) /
  `oc-...` (OpenClaw). Her kurulum (H1/H2/H3/OpenClaw) kendi kimliğini üretir.
- **Donanım parmak izi bağı**: machine-id/DMI UUID/board-serial/MAC/arch
  (Linux/macOS/Windows). Anahtar başka donanıma kopyalanırsa
  `clone_state=suspected` → mesh 403 RED (fail-closed).
- **rekey**: meşru donanım taşıması → `rekey --confirm`; eski kimlik
  `identity_history.json`'da `superseded_by` ile arşivlenir.
- **Peer defteri (TOFU)**: `peers.json` — ilk görülen ajan kaydedilir;
  agent_id değişmezse anahtar değişimi = taklit RED (`peer_key_mismatch`).
- **İmza doğrulama**: `agent|ts|nonce|method|sha256(body)` Ed25519 imzası,
  ±120s pencere + nonce tekrarı koruması (replay RED).
- **ID = anahtar özeti** zorunlu — uydurma agent_id kabul edilmez
  (`id_key_mismatch`).

### Yeni — Sohbet etiketleme (kullanıcı + ajanlar arası AYRI)
- `u.<agent8>.<kanal>.<peer8>.<ulid>` → kullanıcı sohbeti
- `a.<agent8>~<peer8>.<kanal>.<ulid>` → ajanlar arası (iki taraf simetrik)
- Aynı kapsam idempotent; mesajlar `<conv_id>#<seq>` monoton; içerik
  saklanmaz (sadece sha256+boyut) — `conversations.db` (SQLite/WAL).
- A2A inbox'a `conversation_id` + `message_id` yazılır; imzasız istekler
  defteri kirletmez (`from_verified=False`).

### Entegrasyon
- `agent_mesh_a2a.py`: AgentCard `identity` alanı, `/health` agent_id +
  clone_state, `/identity` endpoint (açık anahtar SIZMAZ), istek imza
  doğrulaması, klon şüphesinde 403.
- `a2a_cli.py`: varsayılan İMZALI gönderim, `--no-sign` geriye uyum,
  `X-Conversation-Id`, yerel sohbet defteri kaydı.
- `sync_motor.py identity show|rekey|fingerprint|conv`.
- `--require-signature` (tüm node'lar güncellenince) imzasız istekleri RED.
- Kimlik modülü olmayan sunucular eski (token) davranışıyla çalışır.

### Test
- test_agent_identity.py: 33 test (klon tespiti, rekey arşivi, imza,
  replay, taklit, TOFU, sohbet idempotansı, seq monotonluğu, ULID).

## [2.1.1] — 2026-08-29 (OceanAPI denetim kapanışı)

### Düzeltilen — 2. TUR (OceanAPI 2. denetim #1-#5 — tombstone/audit kilit)
- TOMBSTONE INDEX (tombstones/<ns>/<rid>.json): silme işlemi hedef dosya
  olmasa da kalıcı kaydedilir; normal yazımda index kontrol edilir —
  index.revision >= gelen.revision ise kayıt YENİDEN OLUŞTURULMAZ
  (resurrection/veri kaybı önlendi). Daha yüksek revision'lu yeni kayıt
  meşru diriltmedir: index temizlenir, kayıt yazılır.
- Eşit revision + farklı hlc tombstone: mevcut kayıt KORUNUR (silme
  kaybeder, conflict sayılır), tombstone index'e yazılır.
- msvcrt.locking append modunda EOF pozisyonunu kilitliyordu (iki süreç
  farklı byte'ları kilitler, dışlamazdı) — lock dosyası 1 byte'a
  genişletilir, seek(0), byte 0 kilitlenir. Kilit alınamazsa 3 deneme
  sonra RuntimeError (fail-closed) — kilitsiz yazım yarışı geri gelmez.
- test_memory_fixes.py: 18 test (14 + 4 tombstone index/resurrection)

### Düzeltilen — ORTAK HAFIZA GÜVENLİK + SAĞLAMLIK (OceanAPI gpt-5.6-sol
### denetim bulguları #1-#8 — tamamı kapatıldı, 14 yeni test)
- scan_payload_for_secrets RECURSIVE: iç içe dict/list değerleri ve
  ALLOWED_VALUE_FIELDS dışındaki alanlar da taranır (önceden sadece üst
  seviye alan adları + `value` alanı taranıyordu — nested secret kaçabiliyordu)
- import_memory_delta artık GELEN her kaydı secret tarar (fail-closed:
  hit → atla + rejected_secret sayacı; export RED'le göndermez ama bozuk/ele
  geçirilmiş node'a karşı import tarafı da savunma yapar)
- Tombstone revision karşılaştırmalı: eski/gecikmiş silme (mevcut rev >
  tombstone rev) YENİ KAYDI SİLMEZ; eşit rev + farklı hlc'de mevcut kayıt
  .tombstone. kopyasıyla korunur
- export delta dosya adı µs + uuid soneki — aynı saniyedeki iki export
  overwrite olamaz (sıralama ts önekinden korunur)
- conflict/tombstone dosya adları µs hassasiyetli — aynı saniyede birden
  çok çakışma birbirinin üzerine yazamaz
- append_audit_event LOCK dosyası üzerinde atomik (fcntl.flock /
  msvcrt.locking) — eşzamanlı yazan iki süreç aynı prev_hash okuyamaz,
  hash-chain kırılamaz; yardımcılar _audit_last_hash/_audit_event_hash/
  _audit_append_line ayrıştırıldı
- memory_pull_import hub listeleme HARD hatasında -1 döner (önceden 0 —
  cron başarı sanıyordu); memory_to_fact_store sqlite bağlantı hatasında
  -1 döner; cmd_memory ikisini de kontrol edip rc=1 döndürür
- test_memory_fixes.py: 14 test (nested secret RED, import secret RED,
  stale tombstone koruması, export/conflict µs dosya adı, audit kilitli
  zincir, cmd_memory hata yayılımı) — rclone/GDrive mock'lu, dokunmaz

## [2.1.0] — 2026-08-29

### Eklenen — ÖNCELİK SINIFLI YEDEK + DOĞRULAMA (C modülü, v2.1)
- sync_retention.py node-bazlı politika: KRİTİK (kernel/patent/scripts/
  hermes/math) 12 ay + 8 hafta; ORTA (research/pcb/sim/openclaw) 8 hafta;
  BÜYÜK (hermes-skills/plugins/hermes-full) 4 hafta. --node filtresi eklendi
- cmd_backup upload sonrası SHA doğrulama: rclone lsjson --hash → GDrive
  hash'i yerel sha256 ile karşılaştırılır (eşleşmezse ⚠ rapor)
- test_retention_cmd.py: 9 test (öncelik eşleme, limitler, karar, SHA verify)

### Eklenen — ORTAK AKIL (E modülü, v2.1)
- sync_common_knowledge.py: GDrive hub üzerinde dağıtık ortak durum +
  görev kuyruğu (GPT-5.6 tasarımı). HLC mantıksal saat, fail-closed.
  - state.json: gdrive:hermes-sync/<user>/shared/state.json — her makine
    kendi bloğunu HLC saatli yazar, tüm makineler okur (read→merge→write)
  - tasks/<task_id>.json: pending→running (claim, tek sahip)→done (yalnız
    sahibi); aynı id RED, başkası sahiplenemez/done yapamaz
  - CLI: state, tasks, task-add, task-claim, task-done
- sync_coordinator.py: tasks + state komutları (list/add/claim/done + json)
- node_agent.py once: run_state adımı (her koşuda state.json HLC bloğu)
- test_common_knowledge.py: 10 test (state merge, create RED, claim tek
  sahip, done sahibi, fail-closed) — rclone mock'lu

### Eklenen — VERSİYON ETİKETLEME (A modülü)
- `versions <node> --tag <etiket>`: en son versiyonu etiketler
  (tags/<tag>.txt: tam dosya adı + SHA256 + ts; rclone lsjson --hash).
  Aynı tag → RED (üzerine yazmaz); geçersiz etiket (^[a-z0-9][a-z0-9._-]{0,63}$) → RED
- `versions <node> --diff v1.tar.gz,v2.tar.gz`: iki versiyon tar üye listesini
  karşılaştırır (rclone cat | tar tzf stream — içerik indirmez); eklenen/silinen
- `rollback --dry-run`: ön-inceleme — değişecek dosya + çakışma sayısı,
  HİÇBİR ŞEY yazmaz (force modunu da hesaba katar)
- test_versions_cmd.py: 8 test (tag yazma/aynı-tag RED/geçersiz-tag RED,
  diff iki tar, rollback dry-run non-destructive, rollback force)

### Eklenen — ORTAK HAFIZA (D modülü, v2.1)
- `memory` komutu: sync_memory.py v0.1 → v1.0 AKTİF bağlandı.
  Akış: export (memory DIF → JSONL delta, secret allowlist RED) →
  push (rclone copy → gdrive:hermes-sync/<user>/shared/memory/) →
  pull/import (uzak deltaları çek, conflict_policy='preserve' ile uygula;
  tombstone kaldırma, eşit revision+farklı hlc → .conflict korunur) →
  fact_store (memory_store.db facts tablosuna INSERT OR IGNORE, dedup)
- `--memory-dir` parametresi (varsayılan ~/.hermes/memory)
- `--dry-run` desteği (hiçbir şey yazmaz)
- node_agent.py `once` döngüsüne `memory` adımı + `--no-memory` flag'i
  (motor v2.1+ gerektirir; eski sürüm no-op)
- test_memory_cmd.py: 14 test (export/secret RED, push mock, pull/import
  conflict, fact_store dedup, dry-run) — gerçek GDrive'a dokunmaz

### Düzeltilen
- sync_memory import'u sys.path'e _HERE eklenerek cwd'den bağımsız yapıldı
- fact_store dry-run mesajı yanıltıcıydı ("+N kayıt" → "[DRY] N aday")
- cmd_versions döngü sonrası return 0 eksikti (dispatch rc=None)

## [1.6.0] — 2026-08-13

### Eklenen — AKILLI KURULUM (Kaynak Farkındalıklı Öneri)
- `probe` komutu: yerel CPU/RAM/disk/GPU kaynaklarını ölçer (nvidia-smi →
  lspci → vulkaninfo), tools kataloğunu tarar, manifest'e `resources` +
  `tools_state` yazar → push ile karşı node'a gider
- `propose` komutu: karşı node'da kurulu araçları KAYNAK KONTROLLÜ öneri
  listesine çevirir. GPU öncelikli sıralama; NVIDIA GPU'suz makinede CUDA
  zorunlu araçlar engelliye düşer; disk/RAM/CPU eşikleri denetlenir
  (DISK_INSUFFICIENT / RAM_INSUFFICIENT / CPU_INSUFFICIENT / GPU_MISSING)
- `apply --tool <ad> [--yes]` komutu: onay sonrası kurulum. Non-destructive
  garantileri: zaten kuruluysa RED (üzerine asla yazma), kaynak yetersizse
  RED, `--yes` yoksa interaktif onay (reddedilirse HİÇBİR ŞEY çalışmaz)
- Config `tools` kataloğu: check/gpu/min_ram_gb/min_disk_gb/min_cpus/install
  alanları (cuda-toolkit, vllm, ollama, docker, zephyr-sdk, kicad-cli,
  arm-none-eabi-gcc, qemu-system-arm, ns3)
- `push` artık kaynak + araç durumunu manifest'e otomatik ekler (eşitleme
  sırasında akıllılık; kurulum asla otomatik değildir)

### Düzeltilen
- GPU tespiti iki katmanlı: genel GPU (lspci/vulkan) ayrı, NVIDIA/CUDA
  (nvidia-smi) ayrı — virtio/VGA gibi CUDA uyumsuz GPU'lar CUDA araçlarını
  önerilmez yapar (fail-closed)

## [1.5.0] — 2026-08-12

### Eklenen — Hermes Agent Eşitleri
- `hermes-sessions` node: bir ajanın oturum bilgisi (PROJECT_STATE + kapanış
  özeti, `scripts/hermes_session_digest.py`) eşlere paylaşılır — diğer ajan
  çekip öğrenir
- `hermes-profile` node: config + cron + plugin manifest (SECRETS hariç)
- `validate_skills.py`: skill aktarım kapısı — SKILL.md varlığı + frontmatter +
  references bütünlüğü (kicad skill'inde 4 kırık referans yakaladı)

## [1.4.0] — 2026-08-13

### Eklenen
- `doctor` komutu: ortam sağlık kontrolü — bağımlılıklar (rclone/git/gh),
  node dizinleri (çoklu `paths` desteği), GDrive remote, GitHub repo erişimi
  (URL normalizasyonu + gh auth), sonuç ✅/❌
- `--dry-run` (push/both): ne yapılacağını gösterir, manifest/GitHub/GDrive'a
  HİÇBİR ŞEY yazmaz — güvenli önizleme
- Sürüm bildirimi: `version` komutu + başlıkta v1.4.0

### Düzeltilen
- GDrive çekme O(n²) performans sorunu: `tarfile.getmembers()` + rastgele
  `extractfile()` gzip'te her üye için baştan açıyordu (geriye seek yok) →
  sekansiyel iterasyon tek geçişte açar. 420MB/5000 dosyalık arşivde
  saatlerce %100 CPU → saniyeler.
- Çakışma tespitinde boyut ön-kontrolü: hedef boyut farklıysa içerik
  okumadan çakışma; içerik aynıysa yeniden yazma yok.
- rclone GDrive pull: `--drive-acknowledge-abuse` eklendi.
- config.json: GitHub repo adı düzeltildi (`cumulus-sync` → `cumulus-sync-motor`).

## [1.3.2] — 2026-08-03

- OceanAPI (gpt-5.6) denetim bulguları kapatıldı: deleted yanlış pozitif →
  SHA çakışma koruması; `run_cmd` shell=False (enjeksiyon); `gh_ensure_repo`
  JSON RC; hassas dizin reddi; çoklu yol SHA kontrolü; rclone copy shell=True
  tırnak bug'ı; sessiz aynı-içerik.

## [1.3.1] — 2026-08-03

- AKILLI BUILD GATE: kernel değişikliği → önce build doğrula, FAIL ise push
  durur + `build_break` manifest kaydı. Pipe RC bug fix (make|tail RC'sini
  yutuyordu).

## [1.3.0] — 2026-08-03

- EVRENSEL: kullanıcı kimliği + makine kimliği
  (`gdrive:hermes-sync/<user>/<machine>/versiyonlu/<node>/<ts>/`), sınırsız
  node (`add-node`), paylaşım (`share`), non-destructive çakışma
  (`.conflict.TS`), secret filtre (`.env`/`*.key`). 8 hazır node. Public repo:
  hermes-sync (MIT).

## [1.2.0] — 2026-08-03

- OpenClaw entegrasyonu: `openclaw` node + makine tespiti + skill paketi.

## [1.1.0] — 2026-08-03

- Çoklu dizin, GDrive pull non-destructive, güvenlik (secret filtre), build
  doğrulama, `list_conflicts`/`nodes`/`select`.

## [1.0.x] — 2026-08-02

- İlk sürüm: GitHub manifest merkezi + GDrive versiyonlu yedek + H1↔H2 sync.

## v2.1.0 (29 Ağu 2026)
- restic incremental backup engine (CDC dedup, snapshot, restore, retention)
- A2A mesh: agent-to-agent (JSON-RPC) — sync/async/canlı(SSE) 3 mod
- Syncthing P2P kanal + akıllı kanal seçici (görev→A2A, dosya→Syncthing, arşiv→GDrive)
- Inbox worker (allowlist görev işleyici) + ortak görev dağıtımı (claim/failover)
- Otomatik keşif (state.json) + ortak akıl (HLC) + ortak hafıza
- FAILOVER: stale task devralma, max_attempts, terminal state koruma
- Paketleme: pip (hermes_sync + CLI), 56 test, GitHub Actions CI
