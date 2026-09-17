# CHANGELOG — Synclave (eski ad: hermes-sync)

## [2.7.9] — 2026-09-17 (fix: geçici rclone hatası "rclone yok" sanılıp GDrive kanalını SESSİZCE kapatıyordu)

- **fix (`sync_motor.rclone_available`): erişilebilirlik kontrolü artık
  retry'li OKUMA yolundan geçer ve ÜÇ DURUMLU tanı döner.** Eski hali
  `run_cmd("rclone version")` çağırıyordu; `version` retry OKUMA kümesinde
  OLMADIĞI için ne retry ne de uzun timeout alıyordu. ÖLÇÜM (fix ÖNCESİ, bu
  depo): `_is_idempotent_read("rclone version")` → **False**;
  `rclone_available()` → `run_cmd("rclone version")` → **retries=0,
  timeout=60** (varsayılan). Sonuç: geçici bir hata (yük altında fork/exec
  gecikmesi, AV taraması, anlık rc≠0) "rclone yok" sanılıyor →
  `gdrive_snapshot` "rclone yok — GDrive snapshot atlandı" deyip ATLIYOR,
  `gdrive_pull_latest` False dönüyordu; yani **GDrive kanalı sessizce
  kapanıyordu**. Yeni `rclone_durum()`: `'ok' | 'yok' | 'belirsiz'` — `'yok'`
  KALICI (binary yok / rc=127 / "no such file"), `'belirsiz'` GEÇİCİ (retry
  tükendi, rc≠0 + ağ işareti). `rclone_available()` = `durum == "ok"` → karar
  değişmezi AYNI (fail-closed: `'ok'` değilse GDrive işlemi atlanır, yazma
  yapılmaz); ayrım yalnız TANI içindir.
- **fix (`_RETRY_READ_TOKENS` / `_RCLONE_READ_COMMANDS`): `version` okuma
  kümesine eklendi** (iki sınıflandırıcıda BİREBİR — drift kapısı
  `test_okuma_kumeleri_kume_olarak_esit`). Yazma veto'su okumadan ÖNCE
  geldiği için `rclone copy version dest` gibi komutlar retry ALMAZ
  (fail-closed korunur; kapı: `test_version_yazma_vetosunu_gecemez`).
- **fix (`cmd_doctor`): "GDrive remote YOK" ile "sorgulanamadı" ayrıldı.**
  Doctor okuması `run_cmd("rclone listremotes", timeout=30, shell=True)`
  (retries=0) idi → geçici hatada SAHTE "GDrive remote YOK" yazıyordu. Artık
  `rclone_read(["listremotes"])` (retry=1, shell yok); rc≠0 ise
  "❓ GDrive remote (rclone): sorgulanamadı (rc=…, retry sonrası — geçici hata
  olabilir)" basılır ve doctor rc=1 kalır (fail-closed).
- **fix (yoklama timeout'u): `_RCLONE_PROBE_TIMEOUT = 60` KISALTILMADI.**
  Eski `run_cmd("rclone version")` çağrısının varsayılanı 60s idi; yoklamayı
  30s'e çekmek yük altındaki makinede yanlış "rclone yok" negatifini geri
  getirirdi. Veri okumaları (lsf/cat/…) spec gereği 180s kalır; bu yalnız
  "çalışıyor mu?" yoklamasıdır (retry ile en kötü durum ~123s). Doctor'daki
  `listremotes` okuması da 30s → 60s'e çıkarıldı (aynı gerekçe). Kapı:
  `test_erisilebilirlik_probe_timeout_kisaltilmadi`,
  `test_rclone_durum_probe_timeoutunu_gonderir`.
- **fix (savunma): `rclone_durum()` sınıflandırma sapmasında ÇÖKMEZ.** `rclone_read`
  okuma kümesinde olmayan komutu `ValueError` ile reddeder; `version` sessizce
  kümeden çıkarsa bu istisna sync koşusunu çökertirdi. Artık yüksek sesle
  (`log.error`) raporlanır ve fail-closed `'belirsiz'` döner. Kapı:
  `test_rclone_durum_siniflandirma_sapmasinda_cokmez`.
- **test: `tests/test_retry.py` 119 → 134** — 15 yeni test: sınıflandırma
  (`version` okuma / yazmada veto), üç durum (ok / belirsiz / yok), retry
  sayısı ölçümü (geçici → 2 deneme, kalıcı → 1), yoklama timeout sözleşmesi,
  doctor iki dal (sorgulanamadı ↔ gerçek YOK) ve **AST kapısı**
  `test_rclone_okuma_cagri_yerleri_retry_ister`: `run_cmd` ile çağrılan bir
  rclone OKUMA komutu `retries=1` taşımıyorsa kırmızı — bu kusur sınıfının
  yapısal tekrarını engeller (yazma komutları muaf). AST kapısının kendisi
  MUTASYON ile kanıtlandı: `retries=1` kaldırıldığında kapı kırmızı
  (`[(1662, 'rclone lsd x/x')]`), geri alındığında yeşil.
- **not:** politika kapsamı DEĞİŞMEDİ — yazma komutlarına (copy/copyto/
  backup/push) retry ASLA yok; çift yazma/kısmi durum riski fail-closed
  korunur. Değişen yalnız OKUMA dayanıklılığıdır.

## [2.7.8] — 2026-09-17 (fix: tanınmayan retention knob değeri retention'ı SESSİZCE kapatıyordu — fail-safe)

- **fix (`sync_motor.cmd_restic_backup`): `SYNC_RETENTION_ORDER` knobu artık
  normalize edilir ve tanınmayan değer varsayılan `both`a düşer.**
  Bağımsız DONE-CHECK denetimi (gpt-5.6-sol) bulgusu; ÖLÇÜM (fix ÖNCESİ, bu
  depo): iki dal (`in ("first","both")` / `in ("last","both")`) tanınmayan
  değerde hiçbirini tutmaz →
  `knob='xyz'` → **0 çağrı**, `'FIRST'` → **0**, `''` → **0**,
  `'first,last'` → **0**. Yani v2.7.5'te kapatılan "retention açlığı"
  (forget hiç çalışmıyor → snapshot birikimi) tek bir yazım hatasıyla
  SESSİZCE geri geliyordu: fail-open. Fix sonrası bu dört değer 2 çağrı
  (fail-safe tam tur: biri döngüden önce, biri sonra); `FIRST`/`LAST` gibi
  büyük harf ve `" Both "` gibi boşluklu değerler doğru normalize edilir.
- **test: 9 yeni davranış testi** —
  `test_retention_knob_normalize_edilir` (3 parametre: FIRST / " Both " / LAST)
  ve `test_retention_taninmayan_knob_fail_safe` (6 parametre: `xyz`, `""`,
  `first,last`, `0`, `true`, `"both "`). Statik kapı
  `test_retention_sira_knobu_gecerli_degerler` yeni sözleşmeye güncellendi
  (default-argümansız okuma + `.strip().lower()` + fail-safe düşüş dalı).
- **test: 2 kenar-durum kapısı** (son denetim önerisi) —
  `test_retention_node_listesi_bosken_de_uygulanir` (tüm node'lar `restic:False`
  olsa da retention çalışır; "node yok" günü birikim sürmesin) ve
  `test_node_dongusu_hatasinda_en_az_bir_retention_turu` (döngü istisnasında
  `first` turu tamamlanmış olur — varsayılanın `both` seçilmesinin gerekçesi
  belgelenmiş sınır olarak sabitlendi).
- **test: 431 → 442** (`tests/` tam suite; twin'de 434 passed + 8 beklenen skip
  — `synclave_kod/` ve kök self-check betiği twin/private klonda yok).
- **not: sıra semantiği değişmedi** — `first` yalnız önce, `last` yalnız sonra,
  `both`/bilinmeyen ikisi de (idempotent); yazma komutlarına retry yasağı ve
  diğer fail-closed kurallar aynen geçerli.

## [2.7.7] — 2026-09-17 (parite + DAVRANIŞ kapısı: retention sıra sözleşmesi artık ölçülüyor)

- **test (parite): twin'de olup public'te EKSİK kalan 3 retention-sıra kapısı
  public'e taşındı.** Ölçüm: `diff -rq synclave/` TEMİZ (kod birebir aynı) ama
  `tests/test_restic_failsoft.py` twin'de 156 satır / public'te 121 satır —
  `test_retention_once_kaynagi_tek_fonksiyon`,
  `test_retention_node_dongusunden_once_cagrilir`,
  `test_retention_sira_knobu_gecerli_degerler` public'te YOKTU. Yani v2.7.5
  retention-açlığı düzeltmesinin kanıt kapısı yalnız private repoda duruyordu;
  public'te sessizce geri alınabilirdi.
- **test (davranış kapısı — bağımsız denetim bulgusu, gpt-5.6-sol):** taşınan
  kapılar kaynak METNİNİ sayar; işlev doğruyken yorum/tırnak/biçim değişimi
  testi kırar ve asıl sözleşmeyi KANITLAMAZ (`both` varsayılanında retention'ın
  döngü DIŞINDA toplam iki kez çağrıldığı ölçülmüyordu). Eklendi:
  `test_retention_sayisi_ve_sirasi_davranissal` (first/last/both/env-yok →
  sırasıyla 1/1/2/2 çağrı + çağrı sırasının node döngüsüne göre ÖNCE/SONRA
  olması) ve `test_retention_node_basina_degil_dongu_disinda` (2 node → 2,
  5 node → 2; sayı node sayısıyla ARTMAZ). Mock'lu koşum: gerçek restic
  çağrılmaz, `_restic`/`_restic_retention` olay kaydıyla izlenir.
- **kanıt (mutasyon):** `if _ret_order in ("first", "both")` dalı kapatılıp
  koşuldu — metin kapıları 9/9 PASS (mutasyonu KAÇIRDI), davranış kapıları
  5 FAIL (yakaladı). Kaynak geri alındı, kök kopya ile birebir doğrulandı.
- **test sayısı: 426 → 431** (`tests/` tam suite, 0 FAIL).

## [2.7.6] — 2026-09-16 (retry: iki ÖLÇÜLMÜŞ kusur — kalıcı istisna retry'i + istisna yutma)

- **fix: `run_cmd` istisna yolunda tanı `err`'e de yazılır (`_exec`).**
  İstisna metni yalnız `out`'a konuyor, `err` BOŞ bırakılıyordu. `_is_transient_rc()`
  kalıcı hataları `err` üzerinden ayırt ettiği için KALICI istisnalar (rclone binary
  yok / izin reddi) geçici sanılıp retry ediliyordu. Ölçüm (fix ÖNCESİ, bu depo):
  `run_cmd("rclone cat gdrive:x/y", retries=1)` + `FileNotFoundError`
  → 3s bekleme + `sync hata: … rc=-1 0.0s retry=1/1` (retry açılmamalıydı).
  Aynı hata sınıfı `sync_common_knowledge._run_rclone`'da DOĞRU yapılıyordu
  (`err = str(e)`) — iki yol artık tutarlı; kapı:
  `tests/test_retry.py::test_istisna_yolu_iki_modulde_ayni_davranir`.
  Dış sözleşme DEĞİŞMEZ (`run_cmd` yalnız `(out, rc)` döndürür).
- **fix: `run_with_retry` istisnayı YUTMUYOR (operatör önceliği).**
  Koşul `i < retries and "Errno" in str(e) or "timeout" in str(e).lower()` idi →
  `(A and B) or C`: SON denemede `timeout` içeren istisna yine retry dalına giriyor,
  döngü bitince fonksiyon **None** dönüyordu (fail-open) ve tanı `retry 2/1` gibi
  yanıltıcı yazıyordu. Ölçüm (fix ÖNCESİ): `run_with_retry(boom, retries=1)`
  → `returned=None`, 2 × 5s bekleme. Düzeltme: sınıflandırma tek kaynaktan
  (`_RETRY_FATAL` / `_RETRY_TRANSIENT`) — kalıcı işaretli istisna İLK denemede,
  bilinmeyen istisna da anında yükselir (fail-closed), geçici işaretli istisna en
  çok `retries` kez denenir ve son denemede YÜKSELİR; asla `None` dönmez.
  Statik kapı: `tests/test_retry.py::test_run_with_retry_oncelik_kapisi`.
- **test: 10 yeni regresyon testi** (kalıcı/geçici istisna ayrımı, izin reddi,
  yutma yasağı, bilinmeyen istisna fail-closed, mutlu yol, iki modül tutarlılık
  kapısı, öncelik statik kapısı, kalıcı veto/rc kapsamı). Toplam: 413 → 423.
- **fix (bağımsız denetim bulgusu — gpt-5.6-sol, risk 3/10): kalıcı veto artık
  TÜM rc değerlerinde geçerli (`_is_transient_rc`).** Önceden veto yalnız
  `rc == -1` yolundaydı; `rc != 0` ve mesajda hem 5xx/geçici hem kalıcı işaret
  birlikte varsa komut GEÇİCİ sayılıp retry ediliyordu (fail-open). Ölçüm
  (fix ÖNCESİ): `_is_transient_rc(1, "no such file or directory - 503 Service
  Unavailable")` → True; `(3, "command not found (temporary failure)")` → True;
  `(1, "permission denied: i/o timeout")` → True. Fix SONRASI üçü de False;
  geçici yollar (503 / connection reset / `rc=-1`) retry almaya devam ediyor —
  kapı: `tests/test_retry.py::test_kalici_veto_tum_rc_degerlerinde_gecerli`.
- **not: yazmaya retry politikası DEĞİŞMEDİ** (v2.1.1'den beri: yalnız idempotent
  okumalar; `copy/copyto/move/sync/delete/...` ASLA retry).

## [2.7.5] — 2026-09-15 (retention node döngüsünden ÖNCE de çalışır — defense-in-depth)

- **feat: `_restic_retention(cfg, dry_run)` ayrıştırıldı + `SYNC_RETENTION_ORDER`
  (first|last|both, varsayılan both).** Hermes cron script timeout'u SABİT 3600s
  (`cron/scheduler.py:1105 _DEFAULT_SCRIPT_TIMEOUT`; job `timeout` alanı script
  koşularında KULLANILMAZ — kanıt: job 2bca1d045851 timeout=7200 iken "Script
  timed out after 3600s"). Node döngüsü ~24 dk ölçüldü (20:01 → 20:25, canlı
  `ps`); sonda kalan forget ilk aç kalan adımdı (snapshot 667 → 725, +58/gün).
  `forget` idempotent olduğundan ve `--keep-daily 7` bugünün snapshot'ını
  koruduğundan ilk adımda da çağrılır → koşu 3600s'te öldürülse bile retention
  TAMAMLANMIŞ olur.
- Kilit süpürme: `restic unlock` (YALNIZCA bayat kilitler; `--remove-all` DEĞİL,
  uzak makinenin canlı kilidine dokunmaz), `--retry-lock` 30m → 2m
  (`SYNC_RETENTION_RETRY_LOCK`), `forget` sınırlı timeout + süre ölçümü
  (`SYNC_RETENTION_TIMEOUT=900`, `SYNC_RETENTION_DRY_RUN`, `SYNC_RETENTION_UNLOCK`).
- **fix: `_restic` zaman aşımı fail-soft.** `TimeoutExpired` yakalanır →
  `(-1, "TIMEOUT Ns <çıktı kuyruğu>")`; önceden traceback çökmesi: rc/rapor/log
  yok, kilit saatlerce tutulurdu.
- **not: retention `--prune` yalnız 04:00'te** (üç makine eşzamanlı prune aynı
  repo'yu kilitler; repo bozulabilir — H2 bulgusu).

## [2.7.4] — 2026-09-14 (retention açlığı kapatıldı + Windows kurulumu düzeltildi)

- **fix: retention açlığı zinciri (`f7edde12`) — ölçülü.** `forget` aylardır
  tamamlanmıyordu; repoda **667 snapshot** birikti (420 keep / 247 remove tasarımı).
  Zincir: 14 node'un node-fazı toplamı ~1706s (GDrive REST gecikmesi; 0 değişiklikte
  bile patent 1:38) → `BUTCE_BACKUP_S=1800`'ün ~%95'i node fazında bitiyor → süre
  dolunca SIGKILL → restic kilidini BIRAKAMAZ → yetim kilit (kanıt: 17:54:56 lock
  `"exclusive":false`, pid 532056 ÖLÜ) → sonraki forget eski `--retry-lock 30m`
  boyunca bekleyip yine öldürülüyordu (`restic unlock` tek başına 54s). Düzeltme:
  `_restic` zaman aşımında **fail-soft** (`TimeoutExpired` yakalanır → `(-1, "TIMEOUT Ns ...")`;
  önceden traceback ile çöküyordu, rc/rapor/log yok), retention'da **yetim kilit
  süpürme** (`restic unlock` — `--remove-all` DEĞİL, uzak makinenin CANLI kilidine
  dokunulmaz), `--retry-lock` 30m → **2m** (`SYNC_RETENTION_RETRY_LOCK`), `forget`'e
  SINIRLI timeout + süre ölçümü (`SYNC_RETENTION_TIMEOUT`, varsayılan 900s),
  `SYNC_RETENTION_DRY_RUN`, `SYNC_RETENTION_UNLOCK`.
- **not: `BUTCE_BACKUP_S` 3000s denemesi GERİ ALINDI (1800s).** Toplam adım bütçesi
  `DIS_KAPI_S=3600`'ü aşamaz — `tests/test_node_agent_butce.py`
  (`test_toplam_butce_diskapinin_altinda`) bunu yakaladı. Retention'ın sığması
  **dış kapı + dış cron timeout artışı** gerektirir → SAHİP KARARI (gerekçe koda yorum
  olarak yazıldı).
- **fix: sürüm üçlüsü 2.7.4'e tamamlandı.** `sync_motor.py` 2.7.4 iken
  `pyproject.toml` ve `synclave/__init__.py` 2.7.3'te kalmıştı → bu depoda sürüm
  kapıları kırmızıydı (`test_surum_tek_kaynak_ve_changelog_ile_uyumlu`,
  `test_paket_surumu_tek_kaynak_pyproject_paket_motor`). Dört kaynak (pyproject +
  paket `__init__` + motor + bu başlık) tek değere bağlandı.
- **docs: Windows kurulumu düzeltildi (README + `docs/windows.md`).**
  `pip install rclone` / `pip install restic` **YANLIŞ yoldu**: ikisi de Go binary'si.
  Canlı ölçüm (pypi.org/pypi): `rclone` → "A robust, typed Python wrapper for the
  rclone CLI" (rclone CLI'sini YİNE ister); `restic` → "A modern, type-safe Python
  library for building robust REST API clients" (yedek motoruyla ALAKASIZ) →
  dokümanı izleyen kullanıcı runtime'da `rclone: command not found` alıyordu. Artık
  `winget install Rclone.Rclone` / `winget install restic.restic`
  (winget-pkgs manifestleri canlı doğrulandı) + resmi binary alternatifi + `version`
  doğrulaması. Ayrıca **restic'in bağlandığı uç nokta** yazıldı
  (`rest:http://127.0.0.1:8443/`, `RESTIC_REPO_URL` ile override) ve **`setx`
  kapsamı** (yalnız yeni süreçler; görev SYSTEM/başka kullanıcı bağlamındaysa token
  görünmez → görev tanımında da verilmeli). README'ye uçtan uca Windows bölümü eklendi.
- **Kapılar:** `tests/test_readme_kurulum.py` (pip tuzağı yasağı + uyarı bağlamı
  serbest; Windows bölümü sözleşmesi; doküman↔kod sapma kapısı) ve
  `tests/test_restic_failsoft.py` (timeout fail-soft, çıktı kuyruğu, `timeout`
  geçişi, `--remove-all` KODDA yasak, `SYNC_RETENTION_*` env, eski `retry-lock 30m`
  regresyonu).
- **Ölçüm (tick sonu):** `405 passed / 8 skipped`, RC=0; twin'in kök/paket kopyaları
  byte-eşit; public (`hermes-sync`/`synclave`) ile ortak modüller byte-eşit.

## [2.7.3] — 2026-09-14 (sınırsız rclone çağrısı kapatıldı + yazmaya-retry ihlali geri alındı)

- **fix: ikiz depo parite kapısı KIRMIZIYDI — public repo, twin'in iki düzeltmesini
  taşımıyordu.** Ölçüm (tick başı): `395 test → 1 failed / 390 passed`
  (`test_kopya_parite.py::test_ikiz_depo_paritesi`, "sync_motor.py: paket ve kök
  kopyaları ayrıştı"). Sapma: kök/paket 3929 satır, twin (`cumulus-sync-motor`,
  `c990413c`+`79d13f02`) 3952 satır, İKİSİ de `2.7.2` beyan ediyordu → kapı
  doğru şekilde kırmızıydı. Taşınan gerçek düzeltme: `cmd_backup` GDrive upload'ı
  **SINIRSIZDI** → throttle'da asılma, cron 3600s SIGTERM, stdout blok-tamponlu
  olduğu için BOŞ log ve saatlerce tutulan kilit.
- **fix: `cmd_rollback` indirmesi de SINIRSIZDI (aynı sınıfın ikinci örneği).**
  Dengeli-parantez taraması 12 `subprocess.run` çağrısının TAMAMINI denetledi;
  yalnız bu çağrıda `timeout` yoktu. 180s sınırı + `TimeoutExpired` yakalama
  eklendi; zaman aşımı **fail-closed**: `rc=1`, geri alma/uygulama aşamasına
  geçilmez, hedef dizine hiçbir dosya yazılmaz (kısmi indirme `finally rmtree`).
- **fix: `cmd_backup` upload'ı YAZMAYA RETRY yapıyordu (denetim bulgusu,
  gpt-5.6-sol).** `for _attempt in (1, 2)` döngüsü `copyto`'yu (uzak hedefe YAZMA)
  tekrar deniyordu — "yazmaya asla retry" ilkesinin ihlali; timeout uzak nesnenin
  durumunu kanıtlamadığı için ikinci yazma belirsiz durumu tekrarlıyordu. Döngü
  kaldırıldı: **tek deneme + 180s**. Retry artık RUN seviyesinde (node TIMEOUT
  işaretlenir, sonraki 90 dk koşusu telafi eder) ve uzak nesne için "yazıldı"
  iddiası üretilmez.
- **fix: `synclave_kod/` (node paketi) bayat kopya.** `h2_dogrula.py` bu flat
  dizinden import ediyor; `sync_motor.py` kanonikten 36 satır gerideydi
  (`test_node_paketi_parite.py`). Kanonikle hizalandı.
- **Kapılar (3 yeni, 395 → 398 test):**
  - `test_her_rclone_subprocess_cagrisi_timeout_tasir` — sync_motor'daki HER
    rclone `subprocess.run` çağrısı `timeout` taşımalı (dengeli-parantez taraması;
    düz regex `os.path.basename(...)` iç içe parantezinde kesip timeout'suz
    çağrıyı "timeout var" sanıyordu — dedektörün kendisi de test edilir).
  - `test_yazma_cagrisi_retry_dongusunde_degil` — **AST kapısı**: YAZMA alt-komutlu
    (`copyto/copy/sync/…`) bir çağrı deneme döngüsü (`for x in (1,2)` / `range` /
    `while`) içinde bulunamaz. Metin taraması bunu yakalayamaz; yapı gerekir.
    Dedektör, kaldırılan ihlali yakaladığı ve `for n in nodes:` gibi normal
    döngüye takılmadığı için ayrıca iki testle kilitlendi.
  - `test_rollback_indirmesi_timeout_ve_fail_closed` — indirme sınırı + tanı.
- **Kanıt:** public `398 passed` (exit 0), twin `390 passed / 8 skipped` (exit 0),
  5 kopya (kök, `synclave/`, `synclave_kod/`, twin kök, twin `synclave/`) ve test
  dosyaları byte-parite; davranışsal kanıt: `TimeoutExpired` → `rc=1` + hedef
  dizine 0 dosya.

## [2.7.2] — 2026-09-14 (build kilidi: "kilit dolu" atlaması gerçekten çalışır hale getirildi)

- **fix: `verify_build` build kilidi port edildi + zaman aşımı yarışı kapatıldı.**
  Twin repo'da (`cumulus-sync-motor`, c6050284) commit edilen `flock` düzeltmesi
  public repo'ya taşınmamıştı; kopya paritesi kapısı yakaladı
  (`test_kopya_parite.py::test_ikiz_depo_paritesi` → 1 failed / 375 passed,
  "sync_motor.py: kök kopyaları ayrıştı"). Taşımada İKİ gerçek kusur ölçüldü:
  - **D1 — zaman aşımı yarışı (fail-closed ihlali):** `flock -w 1800` ile
    `subprocess timeout=1800` EŞİTken flock 75 döndüremeden süreç kill
    ediliyordu; `run_cmd` TimeoutExpired'da `("timeout", -1)` döner, çıktıda
    `RC=` bulunmaz → `build_rc = -1` → FAIL → **push bloke**. Yani düzeltmenin
    engellemeye çalıştığı sahte FAIL geri geliyordu. Artık
    `timeout = kilit beklemesi + BUILD_GRACE` (varsayılan 1800 + 900 = 2700s);
    `timeout > bekleme` değişmezi ayrıca test kapısıdır.
  - **D2 — RC ayrıştırma çökmesi:** cmd.exe `$?` genişletmez → `RC=$?` satırı
    korumasız `int()` ile yakalanmamış `ValueError` üretiyordu (Windows push
    yolu çökerdi). Ayrıştırma fail-closed yapıldı: sayı olmayan RC doğrulanmış
    sayılmaz (FAIL), çökme yok.
  - `CUMULUS_LOCK_WAIT` / `CUMULUS_BUILD_GRACE` bozuk, sıfır veya negatifse
    uyarı loglanır ve güvenli varsayılana düşülür (0 saniyelik bekleme kilidi
    devre dışı bırakırdı).
  - Test: `tests/test_build_kilit.py` — 15 test: rc=75 atlama + uyarı, rc!=0
    kapı, rc=0 PASS, bozuk RC fail-closed, `RC=` satırı yok fail-closed,
    zaman aşımı > bekleme, bozuk env → varsayılan, flock komut biçimi +
    build komutuna retry YASAĞI (`_is_idempotent_read` False).

## [2.7.1] — 2026-09-14 (audit log yolu tek kaynak + KARARSIZ kapı kapatıldı)

- **fix: audit log yolu artık tek kaynaktan** (`sync_memory.audit_log_path`).
  `append_audit_event` log dosyasının YANINA kalıcı `<tarih>.jsonl.lock`
  bırakıyor (29 Ağu atomik "oku+ekle" düzeltmesi). Kökteki iki self-check betiği
  log'u `os.listdir(audit_dir)[0]` ile keşfediyordu; liste sırası dosya
  sistemine bağlı olduğundan bazen **BOŞ kilit dosyası** seçiliyordu:
  `test_sync_memory.py` → `lines[0]` **IndexError**, `test_memory_fixes.py` →
  `_audit_last_hash` boş dosyada `"0"*64` → **yanlış FAIL**.
  Sonuç: kapı KARARSIZDI — retry/backoff (v2.6.x) ve Windows (v2.6.0) işleri
  yeşilken `pytest tests/` rastgele kırmızıya düşüyordu (aynı kod bir makinede
  yeşil, diğerinde kırmızı). Mükerrer tarihli-yol ifadesi iki fonksiyondan
  (append + verify) yardımcıya taşındı: tek kaynak, dizin listeleme YOK.
- **Kapı: `tests/test_audit_log_yolu.py`** — (1) yardımcı `.lock` döndüremez,
  (2) kök neden kanıtı: kilit kardeşi oluşur ve BOŞTUR, (3) eski keşif mantığı
  en kötü sırada deterministik olarak YANLIŞ dosyayı seçer (dosya sistemi
  sırasına güvenilmez), (4) append/verify aynı yolu kullanır, kilit kardeşi
  zinciri bozmaz, (5) zincir bozulması hâlâ yakalanır, (6) betikler listelemeye
  dayalı keşfe geri dönemez (AST denetimi — yorumdaki örnek desen yanlış kırmızı
  üretmez).
- **fix: köprü testleri üretim kilidiyle yarışmıyor (İKİNCİ kararsız kapı).**
  `conversation_bridge` kilit yolu sabittir (`~/.hermes/state/bridge.lock`) ve
  test ortam değişkenleriyle taşınmaz; canlı köprü cron'u (15 dk) tam koşu
  anında kilitliyse `main()` "başka bir köprü çalışıyor" deyip rc=0 ile atlıyor,
  ardından satır assert'leri IndexError/AssertionError veriyordu (ölçüm: tam
  suite 4 failed ↔ tek başına 4 passed). Test kendi geçici kilidine yönlendirildi
  — kilit DIŞARIDAN tutulurken de 4/4 PASS.
- **fix: gün devri (UTC gece yarısı) doğrulaması** — `verify_audit_chain` artık
  isteğe bağlı `log_path` alır (gpt-5.6-sol kritik denetimi bulgusu): yazma ve
  doğrulama AYRI çağrılar olduğundan gece yarısını aşan "yaz + doğrula"
  zincirinde varsayılan yol ertesi günün dosyasını seçiyor ve SAĞLAM zincir
  "log yok" ile kırmızı görünüyordu. Çağıran `audit_log_path` ile ürettiği yolu
  geçirir; varsayılan davranış değişmez (geriye uyumlu). Regresyon testi:
  `test_gun_devri_verify_hedefi_kaymaz`.

## [2.7.0] — 2026-09-14 (akıllı hafıza birleştirme + ilerleme yayını)

- **feat: MEMORY.md / USER.md çakışma kopyası yerine BİRLEŞİR (append).** İki
  makine aynı anda hafıza dosyasına § kaydı eklediğinde pull yolu
  `.conflict.TS` üretiyordu. Artık YENİ kayıtlar mevcut dosyaya eklenir;
  hiçbir makinenin eklediği kayıt kaybolmaz ve çakışma kopyası birikmez.
- **Sertleştirme (gpt-5.6-sol kritik denetimi → 5 bulgu kapatıldı).** İlk
  sürüm birleşik listeyi dosyaya GERİ YAZIYORDU; üç gerçek kayıp riski vardı:
  1. uzunluk filtresi (`len(r) > 20`) kısa geçerli kayıtları siliyordu,
  2. ilk-140-karakter **önek anahtarı** farklı kayıtları tekilleştiriyordu,
  3. read→rewrite arası eşzamanlı append **son-yazan-kazanır** ile siliniyordu.
  Bu sürümde: yalnız-append (`os.replace` yok, tek "a" yazımı) · tekilleştirme
  TAM kayıt metniyle (kısaltma yok) · § ayırıcısı satır-başı + CRLF toleranslı
  (kayıt içindeki '§' bölmez) · her hata yolunda **fail-closed** → çakışma
  kopyası (yerel korunur, uzak yazılmaz, kullanıcı kuralı: "bellek içeriği
  ASLA silinmez").
- **feat: ilerleme yayını** — `~/.hermes/state/sync_progress.json` (atomik
  tmp + `os.replace`): koşu, node, yüzde, geçen süre, ETA, node sonuçları,
  hata. Panel (`sync_web_ui.py`) ve `agent-status` bu dosyayı okur. Yazma
  yolu yan kanaldır: hata yükseltmez, koşuyu düşüremez.
- **fix: restic retention `--retry-lock` 5m → 30m.** H2 (Windows) paralel
  yedek koşusu kilidi tutarken `forget` iki kez başarısız oldu (13-14 Eyl
  kanıtı); 30m pencere kilidi devralır.
- **fix: node kodu paketi (`synclave_kod/`) kanonikle hizalandı.** Node'lara düz
  modül paketi olarak verilen dizinde yedi modülden dördü BAYATTI (sync_motor
  2.2.0 ↔ 2.7.0, a2a_cli 164 ↔ 266 satır, agent_mesh_a2a 440 ↔ 535,
  gpu_agent 181 ↔ 229) ve `h2_dogrula.py` bu dizinden import ediyor. Bu sapma
  sınıfı bu depoyu bir kez üretimde vurmuştu ("kök kopya düzeltmeyi taşımıyor,
  testler paket kopyasını okuduğu için görünmüyordu") → dört dosya hizalandı ve
  `tests/test_node_paketi_parite.py` (8 test) kapısı eklendi; dizin yoksa SKIP.
- **test:** `tests/test_hafiza_merge.py` (kısa kayıt + önek çakışması +
  idempotent + alt-küme + fail-closed + kaynak kapıları), `tests/test_ilerleme.py`
  (yüzde/ETA matematiği + atomiklik + yan-kanal güvenliği) ve
  `tests/test_pull_birlesme_e2e.py` (GERÇEK `gdrive_pull_latest` akışı: rclone
  mock'lu, tar.gz gerçek — birleşme + kopya üretilmeme kanıtı; ayrıca hafıza
  dışı dosyada eski çakışma davranışının korunduğu). Mutasyon kanıtı: birleştirme
  devre dışı bırakıldığında e2e testi ÇÖKER (kayıt kaybı + kopya üretimi
  gözlemlendi) — test kanıt, iddia değil. Ölçülen toplam: **367 passed, 0 skipped**
  (önceki durum: 316 passed + 1 failed; +50 yeni test).
- **Port kaynağı:** özellikler ilk olarak private `cumulus-sync-motor`
  kök kopyasında doğmuştu; bu sürümle kanonik hale getirilip DÖRT kopyada
  (public kök + public paket + private kök + private paket) byte-eşitlendi —
  parite kapısı `sync_motor.py: kök kopyaları ayrıştı` kırmızısı bu şekilde
  kapandı.

## [2.6.3] — 2026-09-13 (CI YEŞİL: iki ortam-bağımlı kırmızı kapatıldı + fail-closed pid)

- **CI ölçümü (GitHub Actions, `test` job):** son üç koşu kırmızıydı
  (18s/18s/15s) ve iki test ortam-bağımlıydı — yerelde (root) yeşil,
  runner'da (root olmayan kullanıcı) kırmızı:
  1. `test_kopya_parite.py::test_ikiz_depo_paritesi` — `Path('/root/…').is_file()`
     runner'da **EACCES/PermissionError YÜKSELTİR** (pathlib yalnız
     ENOENT/ENOTDIR/ELOOP'u yutar) → parity kapısı tüm CI'yi düşürüyordu.
     Fix: ikiz depo erişimi `try/except OSError` ile korunur; okunamıyorsa
     test **SKIP** (parite kapısı yalnız erişilebilir olduğunda hüküm verir).
  2. `test_windows_uyum.py::test_fallback_ileri_tarihli_kayit_fail_closed` —
     testten BAĞIMSIZ **üretim hatası** ölçüldü: `_pid_canli()` `os.kill`
     çağrısında EPERM'i de "ölü" sayıyordu. EPERM = "süreç VAR, sinyal
     yetkimiz yok" → **canlı kilit kaydı devralınabiliyordu (fail-open)**.
     Fix: `except PermissionError: return True` (EPERM = canlı),
     `except ProcessLookupError: return False` (ESRCH = ölü); diğer OSError
     ölü sayılmaya devam eder. Test artık hem root'ta hem runner'da anlamlı.
- **Yeni kapılar:** `tests/test_windows_uyum.py` +2 test —
  `test_pid_canli_izin_hatasi_canli_sayilir` (EPERM/ESRCH/OSError/pid≤0
  sınıflandırması), `test_fallback_canli_yabanci_pid_kaydi_reddeder`
  (uçtan uca: canlı yabancı pid + taze kayıt → kilit devralınmaz).
- **Doğrulama:** yerelde `tests/` **317 passed** (315 → 317);
  ikiz depo (`/root/cumulus-sync-motor`) byte-parite + aynı sürüm.
- **Not:** 2.6.2'de eklenen 11 konumsal-tuzak vakası ve veto kümesi kapıları
  bu sürümde aynen geçerlidir.

## [2.6.2] — 2026-09-13 (RETRY VETO KÜMESİ: ölçülmüş fail-open kapatıldı)

- **Ölçülmüş kaçak (bu depo, fix öncesi):** `_RETRY_WRITE_TOKENS` eksikti;
  adı kümede OLMAYAN mutasyon alt-komutları, KONUMSAL argümanı bir okuma
  sözcüğü olduğunda sınıflandırıcı tarafından okuma sanılıyordu →
  "yazmaya ASLA retry" değişmezi ihlal ediliyordu (fail-open):
    - `rclone moveto cat gdrive:dest`   → eski `True` (MOVE'a retry)
    - `rclone touch status gdrive:p`    → eski `True` (WRITE'a retry)
    - `rclone deletefile cat remote:x`  → eski `True` (DELETE'e retry)
- **Kök neden:** sınıflandırıcı okuma sözcüğünü `toks[1:6]` penceresinde
  arar (bayrak önekli okumalar — `rclone --config <yol> lsf …` — kırılmasın
  diye). Penceredeki KONUMSAL bir `cat`/`status` argümanı, alt-komut adı
  veto kümesinde yoksa okuma gibi görünüyordu.
- **Fix:** veto kümesi, `rclone help` (v1.60.1, makinede ölçüldü) listesindeki
  DURUM DEĞİŞTİREN tüm alt-komutları kapsayacak şekilde genişletildi:
  `moveto, deletefile, mkdir, rmdir, rmdirs, touch, cleanup, dedupe, settier,
  copyurl, rcat, bisync, serve, mount, config, authorize, reconnect,
  backend, rc, rcd, completion, genautocomplete, gendocs, selfupdate`.
  Salt-okuma komutları (`lsf/ls/lsl/lsjson/lsd/cat/check/checksum/hashsum/
  md5sum/sha1sum/size/tree/version/ncdu/obscure/link/cryptcheck/cryptdecode/
  test/help`) kümeye alınmadı. Büyüme yönü bilinçli olarak fail-closed:
  gerektiğinde retry KAYBEDİLİR, yazmaya retry AÇILMAZ.
- **Doğrulama (fix sonrası ölçüm):** konumsal tuzakların tamamı `False`;
  canlı okuma çağrıları (`lsd`/`lsf`/`lsjson`/`cat`, bayrak önekli dâhil)
  retry almaya devam ediyor. `tests/test_retry.py` 86 → 101 test:
  - `test_konumsal_okuma_sozcugu_yazmayi_retry_ettirmez` (11 vaka, iki
    sınıflandırıcı),
  - `test_yazma_kumesi_gercek_rclone_mutasyonlarini_kapsar` (ölçülmüş liste kapısı),
  - `test_okuma_kumesi_gercek_rclone_mutasyonu_icermez`,
  - `test_canli_okumalar_veto_genislemesinden_etkilenmedi` (regresyon),
  - `test_run_cmd_konumsal_tuzakta_tek_deneme` (uçtan uca: tek subprocess).
- **Test toplamı:** `tests/` 300 → **315 passed** (tamamı yeşil).
- **Kapsam sınırı:** retry politikası yalnız SINIFLANDIRICI sınırında
  sertleştirildi; canlı çağrı yerleri (GDrive sürüm listesi ×2, A2A `ping`)
  zaten tek parçalı okumadır, davranışları değişmedi.

## [2.6.1] — 2026-09-12 (RETRY SINIFLANDIRICISI: iki ölçülmüş kaçak kapatıldı)

- **Denetim (bağımsız, OceanAPI gpt-5.6-sol DONE-CHECK turu):** denetçinin
  verdiği örnek (`status a b c d copy` → "True") ÖLÇÜMLE YANLIŞ çıktı —
  'copy' veto penceresinin (toks[1:6]) İÇİNDEDİR ve sınıflandırıcı zaten
  False döner. Denetim yine de iki GERÇEK kaçağı ortaya çıkardı; ikisi de
  kapatıldı (çürütülen hipotez de kayda geçti).
- **Bulgu 1 (ölçüldü):** yazma sözcüğü veto penceresinin DIŞINA çıkabiliyordu:
  `rclone status a b c d e f copy` → eski `True` (**yazma komutuna retry**),
  yeni `False`. Pencere artık yalnız OKUMA sözcüğünü bulmak için kullanılır;
  yazma veto'su parçanın TÜM token'larını tarar.
- **Bulgu 2 (ölçüldü):** `rclone lsf h | xargs -I{} rclone copyto {} dest`
  bileşik komutunun ikinci parçası HİÇ incelenmiyordu: eski `True`, yeni
  `False`. Komut boru/zincir ayıraçlarıyla (`|`, `;`, `&`, `&&`, `||`, yeni
  satır) parçalanır ve HER parça idempotent okuma olmalıdır (fail-closed).
  İkinci parçası okuma olmayan boru hatları (`rclone lsf h | tail -1`) da RED
  edilir — bu, rclone rc'sini yutan pipeline yasağıyla aynı yöndedir.
- **Canlı hata DEĞİLDİ:** retry veren üç çağrı yeri (GDrive sürüm listesi ×2,
  A2A `ping`) tek parçalı okumadır; ölçüm `retries=1` + bileşik komut
  kalıbının kodda hiç geçmediğini gösterdi. Sertleştirme dayanıklılığı değil,
  politika değişmezini ("yazmaya ASLA retry") sınıflandırıcı sınırında kilitler.
- **Kapılar:** `tests/test_retry.py` 75 → 86 test: pencere dışı yazma veto'su,
  bileşik komutta gizli yazma/bilinmeyen parça RED, `run_cmd` boru hattında
  TEK subprocess çağrısı (retry yok), GERÇEK okuma çağrılarının retry'ı
  kaybetmemesi, parçalayıcı kenar durumları + kaynak kapısı (`retries=1`
  çağrı yerleri kabuk ayıracı taşıyamaz — retry sessizce kaybolmasın).

## [2.6.0] — 2026-09-12 (WINDOWS UYUMU: ajan kilidi UYGULANDI + platform geçici dizinleri)

- **Bulgu 1 (kod okuması — ölü sabit + belgede olup kodda OLMAYAN garanti):**
  `node_agent.py` içinde `LOCK = "/tmp/cumulus_sync.lock"` sabiti TANIMLI ama
  hiçbir yerde KULLANILMIYORDU (yalnız tanım + docstring). Docstring satır 28
  "Aynı anda iki ajan aynı hub'a yazamaz (ortak flock /tmp/cumulus_sync.lock)"
  diyordu; gerçekte `write_hub_status()` içindeki `rclone copyto` (hub'a yazma)
  HİÇBİR kilit altında değildi. Ajanın koştuğu makine Windows (H2, Task
  Scheduler) ve orada `/tmp` YOKTUR.
- **Bulgu 2 (kod okuması — platform sabiti):** `sync_motor` geçici yolları
  sabitti: `announce()` → `/tmp/hermes_uploads` | `/tmp`, `gdrive_pull_latest()`
  → `/tmp/sync_pull_<node>/` (3 çağrı). Windows'ta bu yol geçerli sürücünün
  köküne (`C:\\tmp\\...`) çözülür — `/tmp/...` gerçek bir dizin değildir.
- **Düzeltme — ajan kilidi (yeni yetenek):** `_agent_lock_path()` (POSIX `/tmp`,
  Windows `%TEMP%` — motor kilit kuralıyla aynı dizin, AYRI dosya adı
  `cumulus_node_agent.lock`) + `_lock_acquire()` / `_lock_release()`:
  `fcntl.flock` → `msvcrt.locking` (non-blocking). Kilit YALNIZCA `rclone
  copyto` etrafında alınır; kilit doluysa bu adım ATLANIR (bekleme yok —
  sınırsız bekleme ajanı ve zamanlayıcıyı kilitlerdi). Ajan kilidi motor
  kilidinden ayrı olmak ZORUNDA: aynı dosya olsaydı ajan kilit tutarken
  `sync_motor` alt-süreci kendi kilidini alamaz, sync sessizce atlanırdı
  (döngüsel kilitlenme). Kilit API'si hiç yoksa pid kaydı + canlılık + 2 saat
  yaş sınırı (fail-closed); kilit dosyası açılamazsa `KILITSIZ` sentinel ile
  ESKİ davranış korunur (çalışma engellenmez).
- **Düzeltme — platform geçici dizini:** `sync_motor._platform_temp_dir()`
  (Windows: `%TEMP%`/`%TMP%`, POSIX: `/tmp` birebir korunur) `announce()` ve
  `gdrive_pull_latest()` çağrılarına bağlandı.
- **Kapsam sınırı (dürüst):** dosya kilidi yalnızca AYNI dosya sistemindeki
  ajanları koordine eder. Farklı makineler makineye ÖZEL `status.json` yazar
  (paylaşımlı değiştirilebilir dosya yok); eşitleme sırası `sync_motor`'un
  kendi kilidiyle korunur.
- **Kapılar:** `tests/test_windows_uyum.py` 14 → 22 test: Windows/POSIX kilit
  yolu, ajan∩motor kilit dosyası AYRI olmalı, ikinci ajan RED (non-blocking),
  kilit altyapısı yoksa engellemez, kilit dolu iken GDrive yazımı yok, rclone
  istisnasında kilit `finally` ile bırakılır, sabit `/tmp` kalıntısı kaynak
  kapısı (`/tmp/sync_pull_`, `/tmp/hermes_uploads`, `LOCK = "/tmp/` yasak).

## [2.5.2] — 2026-09-12 (RETRY POLİTİKASI: iki sınıflandırıcı sapması + bayrak önekli gizlenme)

- **Bulgu 1 (ölçüldü — aynı politikanın iki uygulaması ayrışmıştı):**
  ```
  sync_motor._RETRY_READ_TOKENS               = {cat,lsf,lsjson,lsd,status,ping,listremotes,direxists,about}
  sync_common_knowledge._RCLONE_READ_COMMANDS = {cat,lsf,lsjson,lsd}
  ```
  Ölçüm: `rclone status` motorda **OKUMA** (retry açık), ck'da yazma sanılıp
  retry **kapalı**. Log kanıtı (düzeltme öncesi):
  `sync hata: rclone status gdrive:hub rc=1 0.0s retry=0`.
  Canlı hata **değildi** (ck yalnızca `cat`/`lsf` çağırır), ama ck yeni bir
  okuma komutu kullanmaya başlarsa dayanıklılık sessizce zayıflardı;
  `ping`/`listremotes`/`direxists`/`about` ck'da tamamen eksikti.

- **Bulgu 2 (ölçüldü — bayrak önekli yazma okuma sanılabiliyordu):** yazma
  veto'su yalnız `toks[1]` idi. Global bayrak + **değeri** alt-komutun önüne
  geçerse tarama yazmayı OKUMA sanıyordu:
  `rclone --config lsf copy a b` → eski sonuç `True` (**retry açık = çift
  yazma riski**). Aynı körlük ters yönde de vardı:
  `rclone --config <yol> lsf gdrive:hub` → eski sonuç `False` (gerçek okuma
  retry kaybediyordu). Ölçüm: bu kalıp kodda **hiç kullanılmıyordu**.

- **Düzeltme (tek kanonik okuma kümesi + veto penceresi):** ck okuma kümesi
  kanonik 9 tokene eşitlendi; veto penceresi program adından sonraki ilk
  5 argüman (`toks[1:6]`), kural **fail-closed ve öncelikli**: pencerede
  YAZMA varsa RED → yoksa OKUMA varsa kabul → hiçbiri yoksa RED. Bayrak+değer
  ikilisi alt-komutun önünde 2 konumdan fazlasını kaplayamaz.

- **Düzeltme (sapma kapısı — +37 test):** `tests/test_retry.py`:
  okuma kümelerinin **küme eşitliği**, `okuma ∩ yazma = ∅` güvenlik
  değişmezi, okuma ve yazmada iki sınıflandırıcının **karar eşitliği**,
  gizlenmiş okuma sözcüğü adversarial vakaları (`rclone copy status dest`,
  `rclone move cat a b`, …), bayrak önekli yazma/okuma vakaları ve `status`
  retry davranışı.

- **Yazma komutlarına retry YASAK değişmedi** (çift yazma / kısmi durum
  fail-closed korunur).

- **Kanıt:** düzeltme öncesi 7 test kırmızı (log `retry=0`) → sonrasında
  public **278 passed / 0 failed**, ikiz depo **277 passed / 1 skipped**,
  ikiz-depo parite kapısı yeşil.

## [2.5.1] — 2026-09-11 (KİLİT BÜTÜNLÜĞÜ: sahip kaydı korunur + fallback fail-closed)

- **Bulgu 1 (ölçüldü — reddedilen aday sahibin kaydını siliyordu):**
  ```
  fd1 = acquire_lock()   -> True   dosya: "2197392 2026-09-11T17:11:10.438307Z"
  fd2 = acquire_lock()   -> None   (RED doğru)
  sonra dosya boyutu     -> 0      kayıt: ""        (YANLIŞ: kayıt silindi)
  ```
  Kök neden: `acquire_lock` dosyayı `open(..., "w")` ile **kilit kararından
  önce** kesiyordu (`O_TRUNC`). Reddedilen aday hiçbir şey yazmadığı hâlde
  sahibin PID/ts kaydını yok ediyordu → `sync status` / operatör "kim
  kilitli?" bilgisini kaybediyordu.

- **Bulgu 2 (ölçüldü — ölü guard, fail-open):** `fcntl` ve `msvcrt` bulunmayan
  platformda pid-dosyası guard'ı (`getsize > 0`) truncate'ten SONRA okunduğu
  için **hiç tetiklenemiyordu**; canlı bir sahip kaydı dururken bile kilit
  alınıyordu (`acquire_lock() -> fd`), yani iki eşzamanlı koşu birlikte
  yazabilirdi. Ölçüm: 29 baytlık canlı pid kaydı varken `-> ALINDI (HATA)`.

- **Düzeltme:** dosya artık `r+` (mevcut) / `w+` (ilk oluşturma) ile açılır —
  **kesme yok**; kaybeden aday hiçbir bayt yazmaz. Sahip kaydı kilit
  **alındıktan sonra** ve **sabit genişlikte** (64 bayt, boşluk dolgu) yazılır
  → eski kayıttan artık kalmaz, dosya koşu başına büyümez. Kilit API'si
  yoksa pid guard'ı **dosya açılmadan önce** okunur (canlı pid → RED).
  Windows yolunda kilitlenecek aralık yoksa (0 bayt) önce dolgu yazılır,
  `msvcrt.locking(..., 1)` semantiği değişmez.

- **Kayıt yazımı kilidi düşürmez:** `_kilit_kaydi_yaz` hata verse bile kilit
  korunur (disk/izin sorunu sync'i tümden reddettirmemeli — kayıt yalnız
  teşhis bilgisidir). Regresyon: `test_kayit_yazimi_basarisizsa_kilit_dusmez`.

- **Regresyon testleri (5 yeni, `tests/test_windows_uyum.py` §e):**
  `test_red_edilen_aday_sahibin_kaydini_silmez` (bulgu 1),
  `test_fallback_canli_pid_reddeder` (bulgu 2, fail-closed),
  `test_kilit_kaydi_sinirli_buyume` (sabit genişlik),
  `test_kayit_yazimi_basarisizsa_kilit_dusmez`,
  `test_msvcrt_mevcut_kayit_korunur_ve_buyumez` (Windows yolu).
  Public 234 test PASS; private ikiz depoda aynı ağaç.

- **Bulgu 3 (ÜRETİM — ölçüldü: iç bütçe dış kapıya eşitti → teşhissiz ölüm):**
  `node-agent-otonom` cron'u (8b1a738e790c, 90 dk) **14 ardışık koşuda** düştü:
  `last_status=error`, `last_error="Script timed out after 3600s"`,
  `failure_streak=14` ve `/tmp/node_agent.log` **BOŞ (0 bayt)** — teşhis yok.
  Ölçüm: motorun kendi kaydı `both rc=0` (19:19:53 yerel, koşu 19:13'te başladı
  → sync 6.7 dk), ardından **backup 53+ dk** sürdü ve 3600s'te cron script'i
  öldürdü. Kök neden: `node_agent.run_backup()` iç zaman aşımı `3600s` — dış
  kapıya EŞİT; adım kendi "TIMEOUT" satırını hiçbir zaman yazamadı. İkinci
  katman: çıktı dosyaya yönlendirilince Python stdout'u BLOK tamponlar → ölen
  koşu boş log bırakır.

- **Düzeltme (node_agent v2.5.1):** adım bütçeleri dış kapının ALTINA indirildi
  ve adlandırıldı — `BUTCE_SYNC_S=900`, `BUTCE_BACKUP_S=1800`,
  `BUTCE_MEMORY_S=240`, `BUTCE_DIGER_S=300`, `BUTCE_VARSAYILAN_S=1800`,
  `BUTCE_KISA_S=120` (toplam 3240s < `DIS_KAPI_S=3600s`); motor çağrılarındaki
  çıplak sabitler kaldırıldı. Aşan adım artık `rc=-1` ile **rapor edilir** ve
  koşu state/rapor adımlarına devam eder. `stdout/stderr` satır tamponlu
  (`reconfigure(line_buffering=True)`) + her adım `▶ başladı / ◀ bitti rc süre`
  satırı yazar ve `status.json`'a `sure_s` alanı girer → hangi adımın takıldığı
  log'un son satırından görünür. Cron sarmalayıcısı `python3 -u` ile çalışır
  (`scripts/run-node-agent.sh`, artık depoda sürümlü).

- **Regresyon kapısı (`tests/test_node_agent_butce.py`, 7 test):** her bütçe <
  dış kapı, toplam bütçe < dış kapı (aşarsa koşu yine teşhissiz ölür), motor
  çağrılarında çıplak `timeout=` yasağı (KOD; yorumlar muaf), satır tamponu
  zorunlu, gerçek timeout `rc=-1 + "TIMEOUT 1s"` üretir, `_adim` süre ölçer ve
  adım hatasını yuta. Negatif kontrol: kapı **eski koddaki 5 çağrının tamamını**
  reddeder. Public 241 test PASS (229 temel + 5 kilit + 7 bütçe), private ikiz
  240 PASS + 1 beklenen skip.

- **Bağımsız denetim:** OceanAPI (gpt-5.6-sol) 3 denemede HTTP 504 verdi →
  ücretsiz denetçi Nemotron-3-Ultra-550B (NVIDIA) ile denetlendi; iki kök
  neden doğrulandı, kayıt-yazımı riski (R5) kapatıldı. Denetçinin
  "`msvcrt.locking` 1 bayt → truncate sonrası kilit görünmez olur" iddiası
  **kabul edilmedi** (kilit aralığı truncate ile düşmez; kanıtsız) — bunun
  yerine truncate tamamen kaldırıldı, böylece tartışma konusu ortadan kalktı.
  DONE-CHECK öncesi son denetim turu (aynı gün) 504/503 ile alınamadı —
  kilit düzeltmesi için yapılan önceki tur ve yedi testlik yerel kapı esas
  alındı; **raporda açıkça belirtildi**.

- **AÇIK KALEM (düzeltilmedi, yalnız görünür kılındı):** backup adımı neden 53+
  dk sürüyor (restic/GDrive) — kök neden araştırması ayrı iş. Ayrıca üretim
  kanıtı: `/tmp/cumulus_sync.lock` 11 Eyl 19:24'te **0 bayt** kalmıştı (kilit
  çekişmesinde kaybeden adayın sahibin kaydını sildiğinin canlı izi);
  düzeltmeden sonra aynı çekişmede dosya 64 baytlık kayıtla kalmalı —
  sonraki çekişmeli koşuda doğrulanacak.

- **Kapsam dışı bırakılan (kayıtlı):** `smart_sync.py` (kök, eski motor) aynı
  `open(..., "w")` desenini taşır; hiçbir cron/systemd birimi tarafından
  çağrılmıyor (ölü kod). Yeniden etkinleştirilirse aynı düzeltme şart.
  `conversation_bridge._acquire_lock` etkilenmez (`os.open(O_CREAT|O_RDWR)`,
  truncate yok).

## [2.5.0] — 2026-09-11 (KOPYA PARİTESİ: tek kanonik motor + sapma kapısı)

- **Bulgu (gerçek sapma):** `sync_motor.py` dört kopyada FARKLI içerikteydi —
  public kök 2.2.0, public paket 2.3.2, private kök ve private paket 2.4.2.
  Üretim kök kopyası (cron'un çalıştırdığı `/root/cumulus-sync-motor/sync_motor.py`)
  v2.3.2'nin GDrive düzeltmesini TAŞIMIYORDU: `rclone lsd … | wc -l` /
  `| tail -1` pipeline'ları rc'yi `wc`/`tail`'den alıyordu → gerçek rclone
  rc'si kayboluyor, retry hiç tetiklenmiyor, ağ hatası "versiyon yok" gibi
  görünüyordu. Testler yalnız paket kopyasını okuduğu için sapma görünmezdi
  (yeşil test + bozuk üretim).
- **Birleştirme:** kanonik = üretim kopyası ∪ diğer kopyaların eksik parçaları.
  Gelen: `_lsd_names()` (pipeline'sız, CRLF toleranslı, son-timestamp seçimi),
  `_RETRY_READ_TOKENS` genişletmesi (`listremotes`/`direxists`/`about`),
  hash önbelleği + olay akışı (`_sha_cached`/`_log_event`) ve `cmd_identity`.
  Dört kopya artık byte-eşit (tek SHA-256).
- **Kapsam genişletildi — tüm ortak modüller:** aynı sapma sınıfı 13 ortak
  modülün 4'ünde daha bulundu ve kapatıldı: `node_agent.py` (kök kopyada
  `run_state`/`run_tasks` yoktu → ortak akıl + görev/failover üretimde devre
  dışıydı), `inbox_worker.py` (pakette `_peer_ping`, `kontrol`, `temizle`
  yoktu), `conversation_bridge.py` (pakette Windows `msvcrt` kilidi ve şema
  toleranslı `PRAGMA` okuma yoktu), `gpu_agent.py` (pakette gömme-model
  dışlayan `_pick_model` yoktu → `/api/generate` boş yanıt riski). İki depoda
  13/13 ortak modül byte-eşit.
- **Regresyon kapısı (`tests/test_kopya_parite.py`):** ortak modüllerin TAMAMI
  için kök ↔ paket byte eşitliği (otomatik numaralandırma — yeni modül eklense
  de kapsar), zorunlu sembol birleşimi, pipeline kalıntısı yasağı, tek
  `__version__` + CHANGELOG uyumu, ikiz depo (private) parite kontrolü.
  Negatif kontrol: `sync_motor.py` ve `node_agent.py` mutasyonlarında kapı kırmızı.
- **Bağımsız denetim (Nemotron-3-Ultra, $0) bulgusu — kapatıldı:** `rclone lsd`
  çıktı sırası garanti DEĞİL; eski kod çıktının son satırını "en yeni sürüm"
  sayıyordu (karışık sırada yanlış/eski sürüm çekilebilirdi). Yeni seçim
  `YYYYMMDD_HHMMSS` biçimli adlar içinden `max()` — sıradan bağımsız;
  biçimsiz dizin adları sessizce elenir, hepsi geçersizse warning + fail-closed.
  Regresyon: `test_gdrive_pull_latest_cikti_sirasi_garanti_degil` (karışık
  sıralı + CRLF + gürültü satırlı çıktı) ve `..._tamamen_gecersiz_liste_uyarir`.
- **İkinci denetim turu bulgusu — kapatıldı:** ilk düzeltmedeki
  `len(n) == 15 and n.replace("_","").isdigit()` yalnız UZUNLUK sayıyordu;
  `20260911070000_` (alt çizgi sonda, 15 karakter, sözlük sırası büyük) filtreyi
  geçip `max()` ile YANLIŞ sürümü seçtiriyordu — negatif kontrolle kanıtlandı.
  Yerine tam biçim kapısı: `_SURUM_ADI_RE = ^\d{8}_\d{6}$`
  (`test_gdrive_pull_latest_gecersiz_bicim_elener`).
- **Paket sürümü:** `pyproject.toml` iki depoda hizalandı (public 1.0.1,
  private 2.4.1 → **1.0.2**); PyPI hattı tek (synclave).
- Test: public 216 PASS, private 219 PASS.

### Ek — 11 Eyl 2026, ikinci tur (sessiz doğrulama açıkları)

- **SÜRÜM DÜZELTMESİ (önceki karar revize):** 1.0.2 hizalaması PyPI'daki
  YAYINLANMIŞ en yüksek sürümün (2.4.1, 8 Eyl) ALTINDA kalıyordu; bu ağaçtan
  yayın yapılsa `pip install synclave` yeni sürümü ÇEKMEZDİ (resolver en
  yükseği seçer). Dört kaynak tek değere bağlandı: `pyproject`, paket
  `__init__.__version__`, `sync_motor.__version__`, `CHANGELOG[0]` = **2.5.0**.
  Kanıt: wheel build → `synclave-2.5.0-py3-none-any.whl`, METADATA `Version: 2.5.0`.
  Kapı: `test_paket_surumu_tek_kaynak_pyproject_paket_motor` (negatif kontrol:
  pyproject 1.0.2'ye çekilince kırmızı).
- **KOŞMAYAN DOĞRULAMA KAPATILDI:** kökteki 6 self-check betiği
  (`test_memory_cmd`, `test_memory_fixes`, `test_retention_cmd`, `test_sec_fixes`,
  `test_sync_memory`, `test_versions_cmd` — 74 doğrulama) hiçbir runner
  tarafından çağrılmıyordu. `tests/test_legacy_scripts.py` bunları subprocess ile
  koşturur: `rc=0` **ve** `<N> PASS` özeti, hata işareti yasağı. Negatif
  kontroller: betik kaybolunca kırmızı, `rc=0` + özet yok → kırmızı.
- **PRIVATE DEPODA HİÇ TEST KOŞMUYORDU (gerçek bulgu):** betikler private
  kopyada `tests/manual/` altında; pytest bunları COLLECT edince modül
  seviyesindeki `sys.exit(0)` yüzünden `INTERNALERROR: SystemExit: 0` →
  `no tests collected`. Çözüm: `tests/manual/conftest.py` → `collect_ignore_glob`
  (pytest global `norecursedirs` varsayılanları EZİLMEZ). Private suite artık
  koşuyor: 228 PASS, 1 skip.
- **BETİK YERLEŞİMİ:** iki depodaki kopyalar ayrışmıştı; private kopyadaki
  `dirname(dirname(...))` bir fazla seviye gösterdiği için betikler private'ta
  `ModuleNotFoundError` ile hiç koşamıyordu. `sys.path` bloğu yerleşimden
  bağımsız hale getirildi (işaret dosyası `sync_motor.py` yukarı doğru aranır;
  kök + `<kök>/synclave` eklenir) ve iki depo kopyaları byte-eşitlendi
  (`test_self_check_betikleri_ikiz_depo_ile_ayni`).
- Test: public **229 PASS**, private **228 PASS + 1 skip**.

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
