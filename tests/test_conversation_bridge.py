#!/usr/bin/env python3
"""conversation_bridge testleri — state.db mock ile köprü davranışı"""
import hashlib
import json, os, shutil, sqlite3, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agent_identity as AI
import conversation_bridge as CB


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bridge_test_"))
        self.idir = self.tmp / "ident"
        self.sdb = self.tmp / "state.db"
        self.wm = self.tmp / "wm.json"
        os.environ["AGENT_IDENTITY_DIR"] = str(self.idir)
        os.environ["HERMES_STATE_DB"] = str(self.sdb)
        os.environ["BRIDGE_WATERMARK"] = str(self.wm)
        # mock state.db
        c = sqlite3.connect(str(self.sdb))
        c.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, user_id TEXT,
                              chat_id TEXT, title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                              content TEXT, timestamp REAL, token_count INTEGER,
                              active INTEGER, tool_name TEXT);
        """)
        c.execute("INSERT INTO sessions VALUES('s1','telegram','729504083','123','TestSohbet')")
        c.execute("INSERT INTO sessions VALUES('s2','cli','root','','CLI')")
        self.c = c
        self.ident = AI.AgentIdentity.load_or_create()
        # 14 Eyl 2026: kilit yolu SABİTTİR (~/.hermes/state/bridge.lock) ve test
        # ortam değişkenleriyle taşınmaz. Canlı köprü cron'u (15 dk) tam o sırada
        # koşarsa main() "başka bir köprü çalışıyor" deyip rc=0 ile ATLAR; satır
        # assert'leri IndexError/AssertionError verir → suite RASTGELE kırmızı
        # (ölçüldü: tam suite 4 failed ↔ tek başına 4 passed). Test kendi geçici
        # kilidine yönlendirilir: üretim kilidiyle YARIŞMAZ, kapı deterministik.
        self._lock_orig = CB._LOCK_FILE
        CB._LOCK_FILE = str(self.tmp / "bridge.lock")
        # addCleanup: setUp bu satırdan SONRA patlarsa unittest tearDown'ı
        # ÇAĞIRMAZ ve modül globali geçici değerde kalırdı (gpt-5.6-sol denetimi)
        # — geri yükleme addCleanup ile garanti altına alınır.
        self.addCleanup(self._kilit_geri_yukle)

    def _kilit_geri_yukle(self):
        CB._LOCK_FILE = self._lock_orig

    def tearDown(self):
        for k in ("AGENT_IDENTITY_DIR", "HERMES_STATE_DB", "BRIDGE_WATERMARK"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, sid, role, content, mid, active=1, tok=None):
        self.c.execute("INSERT INTO messages(id,session_id,role,content,timestamp,token_count,active) VALUES(?,?,?,?,?,?,?)",
                       (mid, sid, role, content, mid, tok, active))
        self.c.commit()

    def test_kopru_mesajlari_deftere_akar(self):
        self._add("s1", "user", "merhaba H1", 1)
        self._add("s1", "assistant", "merhaba Ahmet", 2)
        self._add("s1", "user", "hafıza testi 12345", 3, tok=42)
        rc = CB.main(["--full"])
        self.assertEqual(rc, 0)
        rows = AI.list_conversations(self.ident.runtime)
        self.assertEqual(len(rows), 2)  # telegram + cli session
        tg = [r for r in rows if r["channel"] == "telegram"][0]
        self.assertEqual(tg["msg_count"], 3)
        self.assertTrue(tg["conv_id"].startswith("u."))
        self.assertEqual(tg["kind"], "user")
        # içerik saklanmaz
        db = sqlite3.connect(str(AI.conv_db_path(self.ident.runtime)))
        sha = db.execute("SELECT sha256 FROM messages WHERE conv_id=? AND seq=3",
                         (tg["conv_id"],)).fetchone()[0]
        self.assertEqual(sha, hashlib.sha256("hafıza testi 12345".encode()).hexdigest())
        raw = AI.conv_db_path(self.ident.runtime).read_bytes()
        self.assertNotIn("12345".encode(), raw)

    def test_watermark_incremental(self):
        self._add("s1", "user", "bir", 1)
        CB.main(["--full"])
        # watermark işlendi: s1 → 1
        wm = json.loads(Path(str(self.wm)).read_text())
        self.assertEqual(wm.get("s1"), 1)
        # yeni mesaj ekle, köprü sadece onu işler
        self._add("s1", "user", "iki", 2)
        CB.main([])
        rows = AI.list_conversations(self.ident.runtime)
        tg = [r for r in rows if r["channel"] == "telegram"][0]
        self.assertEqual(tg["msg_count"], 2)
        # aynı mesajı tekrar işlememeli (watermark ilerledi)
        CB.main([])
        tg2 = [r for r in AI.list_conversations(self.ident.runtime) if r["channel"] == "telegram"][0]
        self.assertEqual(tg2["msg_count"], 2)

    def test_kanal_ve_kullanici_karismaz(self):
        self._add("s1", "user", "tel", 1)
        self._add("s2", "user", "cli", 2)
        CB.main(["--full"])
        rows = AI.list_conversations(self.ident.runtime)
        chans = {r["channel"] for r in rows}
        self.assertEqual(chans, {"telegram", "cli"})
        # aynı kullanıcı farklı kanal = farklı sohbet
        convs = [(r["channel"], r["peer_id"]) for r in rows]
        self.assertEqual(len(set(convs)), 2)

    def test_aktif_olmayan_mesaj_islenmez(self):
        self._add("s1", "user", "aktif", 1)
        self._add("s1", "user", "pasif", 2, active=0)
        CB.main(["--full"])
        tg = [r for r in AI.list_conversations(self.ident.runtime) if r["channel"] == "telegram"][0]
        self.assertEqual(tg["msg_count"], 1)


if __name__ == "__main__":
    import hashlib
    unittest.main(verbosity=2)
