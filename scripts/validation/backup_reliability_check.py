"""Offline fault-injection checks for the backup readback/lock repair.

Uses temporary workspaces, real encryption + file locks, HTTPX MockTransport,
no GitHub credentials, remote writes, package downloads or personal data.
Run from the checkout: python scripts/validation/backup_reliability_check.py
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import httpx
from applypilot.backup import common, service, transfer, runtime
from applypilot.onboarding.storage import write_json, read_json
from applypilot.onboarding.browser_runtime import BrowserSetupError, failure

CLIENT = httpx.Client
DATA = bytes(range(256)) * 800
HASH = hashlib.sha256(DATA).hexdigest()


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield DATA[:65536]
        raise httpx.ReadTimeout("simulated signed-url-sensitive failure")


class TransportChecks(unittest.TestCase):
    def download(self, handler, target, **kw):
        with patch.object(transfer.httpx, "Client", side_effect=lambda **cfg: CLIENT(transport=httpx.MockTransport(handler), **cfg)), patch.object(transfer.time, "sleep"):
            return transfer.download_asset("private-token", "owner/private", 17, target, len(DATA), expected_sha256=HASH, **kw)

    def test_timeout_resumes_range_without_auth_leak(self):
        seen = []
        def handler(r):
            seen.append(r)
            if r.url.host == "api.github.com":
                self.assertEqual(r.headers.get("authorization"), "Bearer private-token")
                return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/private?secret=signed"})
            self.assertNotIn("authorization", r.headers)
            if "range" not in r.headers:
                return httpx.Response(200, stream=BrokenStream())
            self.assertEqual(r.headers["range"], "bytes=65536-")
            return httpx.Response(206, headers={"content-range": f"bytes 65536-{len(DATA)-1}/{len(DATA)}"}, content=DATA[65536:])
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"readback"
            self.download(handler,path)
            self.assertEqual(path.read_bytes(), DATA)
            self.assertFalse(path.with_name("readback.part").exists())
        self.assertEqual(len(seen),4)

    def test_server_ignoring_range_restarts_not_appends(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"asset";path.with_name("asset.part").write_bytes(DATA[:65536])
            self.download(lambda r:httpx.Response(200,content=DATA),path)
            self.assertEqual(path.read_bytes(),DATA)

    def test_invalid_resume_range_never_installs(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"asset";path.with_name("asset.part").write_bytes(DATA[:65536])
            with self.assertRaisesRegex(ValueError,"resume range"):
                self.download(lambda r:httpx.Response(206,headers={"content-range":f"bytes 0-{len(DATA)-1}/{len(DATA)}"},content=DATA),path)
            self.assertFalse(path.exists())

    def test_bad_hash_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"asset";path.write_bytes(b"previous verified contents")
            with self.assertRaisesRegex(ValueError,"checksum"):
                self.download(lambda r:httpx.Response(200,content=b"x"*len(DATA)),path)
            self.assertEqual(path.read_bytes(), b"previous verified contents")

    def test_retries_bounded_and_partial_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"asset"
            def handler(r):
                if "range" in r.headers:raise httpx.ReadTimeout("simulated secret URL")
                return httpx.Response(200,stream=BrokenStream())
            with self.assertRaises(transfer.BackupTransferError) as ctx:
                self.download(handler,path,attempts=2)
            self.assertEqual(ctx.exception.details["attempts"],2)
            self.assertEqual(path.with_name("asset.part").stat().st_size,65536)
            self.assertNotIn("private-token",json.dumps(ctx.exception.details))
            self.assertNotIn("simulated secret URL",json.dumps(ctx.exception.details))
            self.assertFalse(path.exists())

    def test_redirect_to_untrusted_host_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,"unexpected GitHub"):
                self.download(lambda r:httpx.Response(302,headers={"location":"https://githubusercontent.com.attacker.invalid/private"}),Path(d)/"asset")

    def test_long_retry_after_does_not_retry_early(self):
        calls=[]
        def handler(r):calls.append(r);return httpx.Response(429,headers={"retry-after":"3600"})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(transfer.BackupTransferError) as ctx:
                self.download(handler,Path(d)/"asset",total_seconds=60)
            self.assertEqual(len(calls),1)
            self.assertEqual(ctx.exception.details["retry_after_seconds"],3600)

    def test_auth_failure_is_not_retried(self):
        calls=[]
        def handler(r):calls.append(r);return httpx.Response(401)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,"401"):
                self.download(handler,Path(d)/"asset")
        self.assertEqual(len(calls),1)


class FakeRemote:
    def __init__(self):
        self.meta=None;self.head_value=None;self.tag=None;self.parts={};self.uploads=0;self.creates=0;self.publishes=0
        self.fail_read_once=False;self.fail_write_once=False;self.fail_create_once=False
    def require_write_access(self):pass
    def close(self):pass
    def head(self):return self.head_value
    def metadata(self,head=None):return self.meta
    def ensure_initial(self,meta):self.meta=meta;self.head_value="a"*40
    def find_release(self,tag):return 10 if self.tag==tag else None
    def create_release(self,tag,head):
        self.tag=tag;self.creates+=1
        if self.fail_create_once:
            self.fail_create_once=False
            raise httpx.ReadTimeout("ambiguous create")
        return 10
    def assets(self,release):
        return {name:{"id":part[0],"size":len(part[1]),"state":"uploaded","digest":"sha256:"+hashlib.sha256(part[1]).hexdigest()} for name,part in self.parts.items()}
    def upload(self,release,path):
        self.uploads+=1;self.parts[path.name]=(100+len(self.parts),path.read_bytes())
        if self.fail_write_once:
            self.fail_write_once=False
            raise httpx.ReadTimeout("ambiguous upload")
    def download(self,asset,target,size,**kw):
        if self.fail_read_once:
            self.fail_read_once=False
            raise transfer.BackupTransferError(received=1,expected=size,attempts=4,reason="ReadTimeout")
        data=next(v[1] for v in self.parts.values() if v[0]==asset)
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    def publish(self,release):self.publishes+=1
    def commit(self,files,meta,head):self.meta=meta;self.head_value="b"*40;return self.head_value
    def releases(self):return []


class BackupChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        key=base64.urlsafe_b64encode(b"k"*32).decode()
        (self.root/".env").write_text(f"APPLYPILOT_BACKUP_KEY='{key}'\nAPPLYPILOT_ALERT_WEBHOOK=''\n")
        write_json(common.folder(self.root)/"config.json", common.DEFAULTS|{"enabled":True,"repository":"owner/private","chunk_mb":16})
        write_json(self.root/"profile.json",{"name":"Offline example"})
        self.remote=FakeRemote()
    def tearDown(self):self.temp.cleanup()
    def sync(self):return service.sync(self.root,full=True,remote_factory=lambda *a:self.remote)
    def test_readback_failure_resumes_without_duplicate_upload(self):
        self.remote.fail_read_once=True
        with self.assertRaises(transfer.BackupTransferError):self.sync()
        self.assertEqual(self.remote.publishes,0)
        self.assertTrue((common.folder(self.root)/"pending.json").is_file())
        result=self.sync()
        self.assertEqual(result["status"],"synced")
        self.assertEqual(self.remote.uploads,2) # one encrypted archive plus manifest, each exactly once
        self.assertEqual(self.remote.creates,1)
        self.assertEqual(self.remote.publishes,1)
        self.assertFalse((common.folder(self.root)/"pending.json").exists())
    def test_ambiguous_upload_reconciles_instead_of_reposting(self):
        self.remote.fail_write_once=True
        with self.assertRaises(httpx.ReadTimeout):self.sync()
        self.sync();self.assertEqual(self.remote.uploads,2);self.assertEqual(self.remote.creates,1)
    def test_ambiguous_release_creation_reconciles(self):
        self.remote.fail_create_once=True
        with self.assertRaises(httpx.ReadTimeout):self.sync()
        self.sync();self.assertEqual(self.remote.creates,1)
    def test_busy_does_not_create_remote_or_failure(self):
        with common.lock(self.root):
            result=self.sync();self.assertEqual(result["status"],"busy")
            self.assertFalse(runtime.due(self.root))
        self.assertEqual(self.remote.creates,0)
        self.assertNotIn("failures",read_json(common.folder(self.root)/"state.json",{}))
    def test_scheduler_respects_backoff_before_spawn(self):
        write_json(common.folder(self.root)/"state.json",{"retry_at":time.time()+1000})
        with patch.object(runtime,"spawn") as spawn:
            self.assertIsNone(runtime.spawn_if_due(self.root));spawn.assert_not_called()
    def test_record_failure_only_once(self):
        exc=RuntimeError("offline")
        service.record_failure(self.root,exc);service.record_failure(self.root,exc)
        self.assertEqual(read_json(common.folder(self.root)/"state.json",{})["failures"],1)
    def test_corrupted_pending_manifest_never_publishes(self):
        self.remote.fail_read_once=True
        with self.assertRaises(transfer.BackupTransferError):self.sync()
        row=read_json(common.folder(self.root)/"pending.json",{})
        path=common.folder(self.root)/"pending-assets"/row["id"]/"applypilot-manifest.json"
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError,"manifest changed"):self.sync()
        self.assertEqual(self.remote.publishes,0)
    def test_readback_bytes_authenticate_and_restore(self):
        from applypilot.backup import archive
        self.sync()
        data=self.remote.parts["applypilot-manifest.json"][1]
        manifest=json.loads(data)
        out=self.root/"offline_restore";out.mkdir()
        for name,(_,content) in self.remote.parts.items():(out/name).write_bytes(content)
        archive.decrypt_parts(out,manifest,out/"snapshot.tar.gz",b"k"*32)
        archive.unpack(out/"snapshot.tar.gz",out/"files",manifest["files"],1000000)
        self.assertEqual(json.loads((out/"files"/"profile.json").read_bytes()),{"name":"Offline example"})


class BrowserChecks(unittest.TestCase):
    def test_missing_runtime_retains_type_not_raw_message(self):
        exc=failure(ModuleNotFoundError("private browser path"),"import")
        self.assertEqual(exc.details["code"],"cloakbrowser_dependency_missing")
        self.assertEqual(exc.details["causes"],["ModuleNotFoundError"])
        self.assertNotIn("private browser path",json.dumps(exc.details))
    def test_profile_lock_and_integrity_are_distinct(self):
        self.assertEqual(failure(RuntimeError("ProcessSingleton"),"launch").details["code"],"cloakbrowser_profile_busy")
        self.assertEqual(failure(RuntimeError("checksum mismatch secret-cookie"),"prepare").details["code"],"cloakbrowser_integrity")
    def test_diagnostics_preserves_structured_phase(self):
        from applypilot.onboarding.diagnostics import explain
        exc=failure(RuntimeError("secret-token"),"launch")
        result=explain(exc)
        self.assertEqual(result["phase"],"launch")
        self.assertNotIn("secret-token",json.dumps(result))


if __name__=="__main__":unittest.main(verbosity=2)
