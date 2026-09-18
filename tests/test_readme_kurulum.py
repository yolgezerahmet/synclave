#!/usr/bin/env python3
"""README + Windows kurulum kapısı (v2.7.4).

Neden: 14 Eyl 2026'da README'nin Windows bölümü kullanıcıyı YANLIŞ kuruluma
yönlendiriyordu:

    pip install rclone      # PyPI'daki 'rclone' bir Python WRAPPER'dır
                            # (rclone CLI'sini yine ister)
    pip install restic      # PyPI'daki 'restic' tamamen ALAKASIZ bir
                            # REST-istemci kütüphanesidir — yedek motoru DEĞİL

rclone ve restic Go binary'leridir; pip resmi dağıtım yolu değildir. Bu satırlar
kalırsa yeni bir Windows kullanıcısı "kurulum tamam" sanır ama motor sessizce
çalışmaz (runtime'da `rclone: command not found`). docs/windows.md doğruyu
söylüyordu (rclone için açıkça), README tersini söylüyordu.

Kapı üç şeyi zorlar:
  1. README/docs'ta `pip install rclone|restic` bir TALİMAT olarak YASAK —
     uyarı bağlamındaki ("pip değil", "KURMAZ") anmalar serbest, çünkü tuzağı
     adıyla göstermek okuyucuya değer katar.
  2. Windows bölümü rclone + restic + syncthing için winget/binary yolunu verir
     ve `rclone version` / `restic version` doğrulamasını içerir.
  3. Windows bölümü A2A token + uvicorn + restic serve + ilk senkron adımlarını
     içerir (kurulumun UÇTAN UCA çalışması için zorunlu adımlar).
  4. (18 Eyl 2026) README'nin BEYAN ETTİĞİ sürüm pyproject ile uyumlu olmalı —
     hem bu depoda hem (erişilebilirse) ikiz depoda. ÖLÇÜLMÜŞ kusur: ikiz
     README'de `pip install synclave==2.4.0` yazıyordu ama kod 2.7.9'du; readme'yi
     izleyen kullanıcı ESKİ sürümü kurardı (pin, resolver'ın "en yükseği seç"
     davranışını da devre dışı bırakır). Sürüm beyanı hiçbir kapıya bağlı
     değildi; bu iki test onu pyproject'e bağlar (boş küme geçmez — kapı
     işlevsizleşemez).
"""
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
WINDOWS_DOC = REPO / "docs" / "windows.md"
# İkiz (private) depo — erişilemezse ilgili test SKIP eder (CI'da /root başka
# kullanıcıya ait olabilir; pathlib orada PermissionError yükseltir).
IKIZ_REPO = Path("/root/cumulus-sync-motor")

# pip ile kurulamayacak Go binary'leri (PyPI'da aynı adlı tuzak paketler var).
PIP_YASAK = ("rclone", "restic")

# Bir satır bu sözcüklerden birini taşıyorsa UYARI bağlamıdır, talimat değil.
UYARI_BAGLAMI = ("kurulmaz", "kurmaz", "kurmayın", "yasak", "pip değil",
                 "değildir", "tuzak", "dikkat")


def pip_yasak_satirlari(icerik: str) -> list:
    """`pip install <go-binary>` içeren ve UYARI bağlamı taşımayan satırlar."""
    kotu = []
    for satir in icerik.splitlines():
        for paket in PIP_YASAK:
            if not re.search(rf"pip\s+install[^\n]*\b{paket}\b(?!\w)", satir, re.I):
                continue
            if any(k in satir.lower() for k in UYARI_BAGLAMI):
                continue
            kotu.append(satir.strip())
    return kotu


def _windows_bolumu() -> str:
    """README'den Windows kurulum bölümü (iki başlık dili de kabul — twin README'de
    `## Windows`, public README'de `### Windows Kurulum`)."""
    metin = README.read_text(encoding="utf-8")
    m = re.search(r"^#{2,3}\s*Windows(?: Kurulum)?[^\n]*\n.*?(?=^#{2,3}\s|\Z)",
                  metin, re.S | re.M)
    assert m, "README'de Windows kurulum bölümü yok"
    return m.group(0)


# ─── Kapının kendisi test edilir (yanlış yeşil koruması) ─────────────
def test_kapi_talimat_satirini_yakalar():
    """Pozitif kontrol: çıplak talimat satırı YAKALANMALI (kapı ölü değil)."""
    assert pip_yasak_satirlari("pip install rclone\n") == ["pip install rclone"]
    assert pip_yasak_satirlari("    pip install restic  # yedek motoru\n")


def test_kapi_uyari_baglamini_serbest_birakir():
    """Negatif kontrol: 'pip değil / KURMAZ' uyarıları serbest (tuzağı gösterir)."""
    assert pip_yasak_satirlari("restic Go binary'sidir; pip değil\n") == []
    assert pip_yasak_satirlari("`pip install restic` yedek motorunu KURMAZ\n") == []
    assert pip_yasak_satirlari("pip install rclone YASAK — binary kur\n") == []


# ─── Gerçek dosyalar ────────────────────────────────────────────────
def test_readme_ve_docs_pip_tuzagini_talimat_olarak_icermiyor():
    for yol in (README, WINDOWS_DOC):
        if not yol.exists():
            continue
        icerik = yol.read_text(encoding="utf-8")
        kotu = pip_yasak_satirlari(icerik)
        assert not kotu, (
            f"{yol.name}: 'pip install rclone|restic' TALİMATI yasak — Go "
            f"binary'leri; PyPI paketleri aracın kendisi değil. Satırlar: {kotu}")
        # Yasak bağlamı: 'pip değil' uyarısı metinde AÇIKÇA bulunmalı.
        assert re.search(r"pip (değil|bu aracın resmi dağıtım yolu değildir)",
                         icerik, re.I), f"{yol.name}: pip uyarısı (binary kur) eksik"


def test_windows_bolumu_binary_kurulumunu_ve_dogrulamayi_icerir():
    """winget/binary yolu + `version` doğrulaması Windows bölümünde olmalı."""
    bolum = _windows_bolumu()
    assert "Rclone.Rclone" in bolum, "winget Rclone.Rclone kimliği yok"
    assert "restic.restic" in bolum, "winget restic.restic kimliği yok"
    assert "Syncthing.Syncthing" in bolum, "winget Syncthing.Syncthing kimliği yok"
    assert "rclone version" in bolum, "rclone version doğrulaması yok"
    assert "restic version" in bolum, "restic version doğrulaması yok"
    # Binary alternatifi (winget yoksa) — resmi indirme adresleri.
    assert "rclone.org/downloads" in bolum
    assert "restic.net" in bolum


def test_windows_bolumu_uctan_uca_adimlari_icerir():
    """Kurulumun çalışması için zorunlu adımlar (auth, token, serve, ilk senkron)."""
    bolum = _windows_bolumu()
    for gerekli in ("rclone config", "serve restic", "A2A_TOKEN",
                    "pip install synclave", "pip install uvicorn fastapi",
                    "sync_motor", "mesh status"):
        assert gerekli in bolum, f"Windows bölümünde eksik adım: {gerekli!r}"


def test_windows_bolumu_restic_uc_noktasini_ve_setx_anlamini_yazar():
    """restic'in BAĞLANDIĞI adres + RESTIC_REPO_URL + setx'in kapsamı yazılmalı.

    OceanAPI (gpt-5.6-sol) denetimi: 'rclone serve restic' başlatmak tek başına
    yetmez — restic istemcisi `rest:http://...` uç noktasına bağlanır; README bunu
    yazmazsa kullanıcı sunucuyu ayağa kaldırır ama motor bağlanamaz.
    """
    bolum = _windows_bolumu()
    assert "rest:http" in bolum, "restic repo URL (rest:http://...) yok"
    assert "RESTIC_REPO_URL" in bolum, "RESTIC_REPO_URL üzerine yazma yok"
    assert "yeni terminal" in bolum.lower() or "system" in bolum.lower(), \
        "setx kapsamı (yeni terminal / SYSTEM bağlamı) uyarısı yok"


def test_windows_doc_restic_uc_noktasi_ve_system_uyarisini_icerir():
    icerik = WINDOWS_DOC.read_text(encoding="utf-8")
    assert "rest:http://127.0.0.1:8443/" in icerik, "docs: varsayılan repo URL yok"
    assert "RESTIC_REPO_URL" in icerik, "docs: repo URL üzerine yazma yok"
    assert "SYSTEM" in icerik, "docs: SYSTEM/başka kullanıcı token uyarısı yok"


def test_dokuman_varsayilan_restic_uc_noktasi_kodla_ayni():
    """Sapma kapısı: dokümandaki varsayılan = koddaki varsayılan (ortam-bağımsız).

    Kod sabiti okunurken runtime değeri DEĞİL, kaynak literal okunur — test
    ortamında RESTIC_REPO_URL tanımlıysa runtime değeri sapardı.
    """
    kaynak = (REPO / "synclave" / "sync_motor.py").read_text(encoding="utf-8")
    m = re.search(
        r'RESTIC_REPO_URL\s*=\s*os\.environ\.get\(\s*"RESTIC_REPO_URL"\s*,\s*"([^"]+)"',
        kaynak)
    assert m, "sync_motor.py'de RESTIC_REPO_URL varsayılanı bulunamadı"
    kod_varsayilan = m.group(1)
    assert kod_varsayilan in _windows_bolumu().replace("`", ""), \
        f"README Windows bölümü koddaki varsayılanı ({kod_varsayilan}) yazmıyor"
    assert kod_varsayilan in WINDOWS_DOC.read_text(encoding="utf-8"), \
        f"docs/windows.md koddaki varsayılanı ({kod_varsayilan}) yazmıyor"


def test_windows_doc_pip_tuzagini_restic_icin_de_yaziyor():
    """docs/windows.md restic bölümünde de 'pip değil' uyarısı olmalı (rclone'da vardı)."""
    icerik = WINDOWS_DOC.read_text(encoding="utf-8")
    m = re.search(r"##\s*3\).*?restic.*?(?=^##\s|\Z)", icerik, re.S | re.M)
    assert m, "docs/windows.md restic bölümü bulunamadı"
    assert "pip değil" in m.group(0).lower(), \
        "restic bölümünde pip uyarısı eksik"


def _pyproject_surumu(pyproject: Path) -> str:
    metin = pyproject.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', metin)
    assert m, f"{pyproject}: [project] version bulunamadı"
    return m.group(1)


def _pinler(metin: str) -> list:
    """`pip install synclave==X.Y.Z` pinleri."""
    return re.findall(r"pip\s+install\s+synclave==([0-9][0-9A-Za-z.\-]*)", metin)


def _pypi_beyanlari(metin: str) -> list:
    """`# PyPI — v2.7.9` biçimindeki sürüm beyanları."""
    return re.findall(r"PyPI\s*[—\-]\s*v([0-9][0-9A-Za-z.\-]*)", metin)


def test_readme_surum_beyani_pyproject_ile_uyumlu():
    """README'nin beyan ettiği sürüm pyproject'ten saparsa kırmızı (boş küme geçmez).

    Neden (18 Eyl 2026, ÖLÇÜLMÜŞ kusur — ikiz depoda bulundu): README
    `pip install synclave==2.4.0` diyordu, kod ise 2.7.9'du. README'yi izleyen
    kullanıcı eski sürümü kurar; güncellemeleri almaz ve PyPI'da yeni sürüm
    varken hata bildirir. Sürüm beyanı hiçbir kapıya bağlı değildi.
    """
    surum = _pyproject_surumu(REPO / "pyproject.toml")
    metin = README.read_text(encoding="utf-8")

    pinler = _pinler(metin)
    beyanlar = _pypi_beyanlari(metin)
    assert pinler or beyanlar, (
        "README'de hiç sürüm beyanı bulunamadı — kapı işlevsiz (boş küme geçmez); "
        "kurulum bloğuna `# PyPI — v<pyproject sürümü>` ekleyin"
    )
    kotu_pin = [p for p in pinler if p != surum]
    assert not kotu_pin, (
        f"README paket pini pyproject ({surum}) ile uyuşmuyor: {kotu_pin}"
    )
    kotu_beyan = [b for b in beyanlar if b != surum]
    assert not kotu_beyan, (
        f"README'nin beyan ettiği sürüm pyproject ({surum}) ile uyuşmuyor: {kotu_beyan}"
    )


def test_ikiz_readme_surum_beyani_kendi_pyprojecti_ile_uyumlu():
    """İkiz (private) README pinleri kendi pyproject'i ile uyumlu olmalı.

    Erişim/uyum koruması `test_ikiz_depo_paritesi` ile aynı desende: ikiz depo
    okunamıyorsa (CI'da /root başka kullanıcıya ait olabilir) veya farklı
    sürümdeyse SKIP — kapı yalnız gerçekten erişilebilir olduğunda hüküm verir.
    """
    if os.environ.get("SYNCLAVE_IKIZ_ZORUNLU", "1") == "0":
        pytest.skip("ikiz depo kontrolü kapatıldı (SYNCLAVE_IKIZ_ZORUNLU=0)")
    try:
        ikiz_readme = IKIZ_REPO / "README.md"
        if not ikiz_readme.is_file():
            pytest.skip("ikiz depo yok (bu makinede private kopya kurulu değil)")
        metin = ikiz_readme.read_text(encoding="utf-8")
    except OSError as e:                    # EACCES/EPERM → ikiz depo okunamaz
        pytest.skip(f"ikiz depo okunamadı ({e.__class__.__name__}) — kapı atlandı")
    if "pip install synclave" not in metin:
        pytest.skip("ikiz README'de kurulum yolu yok — kontrol kapsam dışı")
    surum = _pyproject_surumu(IKIZ_REPO / "pyproject.toml")
    kotu = [p for p in _pinler(metin) if p != surum]
    kotu += [b for b in _pypi_beyanlari(metin) if b != surum]
    assert not kotu, (
        f"ikiz README sürüm beyanı kendi pyproject'i ({surum}) ile uyuşmuyor: {kotu}"
    )
