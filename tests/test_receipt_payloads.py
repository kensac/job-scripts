import gzip
import hashlib
import io
from dataclasses import asdict, replace

import pytest

from api import db
from api.ai import batch_results, receipt_payloads
from core.batch import BatchResult, BatchSpec
from core.payload_objects import PayloadRef, PayloadStore, PayloadUnavailable


class ObjectClient:
    def __init__(self):
        self.objects = {}
        self.fail_put = False
        self.fail_get = False
        self.after_put = lambda: None

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.fail_put:
            raise OSError("upload failed")
        self.objects[Bucket, Key] = Body
        self.after_put()

    def get_object(self, *, Bucket, Key):
        if self.fail_get:
            raise OSError("read failed")
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}


@pytest.fixture
def objects():
    return PayloadStore(ObjectClient(), "test-payloads")


def receipt(f, *, status="done", kind="embed_postings_batch", consumed=True):
    task_id = f.make_task(kind, {}, status=status)
    batch_results.snapshot_specs(task_id, [BatchSpec("packed", endpoint="/v1/embeddings")])
    result = BatchResult(
        "packed",
        batch_id=f"batch-{task_id}",
        embedding_vectors=[[0.1, 0.2], [0.3, 0.4]],
        usage={"input_tokens": 10, "output_tokens": 0},
        model="embedding-model",
    )
    batch_results.checkpoint(task_id, [result], [])
    if consumed:
        with batch_results.consume_result(task_id, result) as acknowledgement:
            acknowledgement.outcome = "written"
    return task_id, result


def row(task_id):
    return db.query_one("SELECT * FROM batch_result_receipts WHERE task_id=%s", (task_id,))


def test_verified_object_round_trip_and_deterministic_identity(objects):
    value = [[0.125, -1.25], [0, 4]]
    ref = objects.put_verified(value)
    assert objects.get(ref) == value
    assert objects.put_verified(value) == ref
    assert len(objects.client.objects) == 1
    assert ref.version == 2
    assert objects.client.objects[ref.bucket, ref.key] == b"[[0.125,-1.25],[0,4]]"
    assert ref.size == len(objects.client.objects[ref.bucket, ref.key])


def test_legacy_gzip_objects_remain_readable(objects):
    raw = b"[[0.125,-1.25],[0,4]]"
    digest = hashlib.sha256(raw).hexdigest()
    ref = PayloadRef(objects.bucket, f"payloads/v1/sha256/{digest}.json.gz", digest, len(raw))
    objects.client.objects[ref.bucket, ref.key] = gzip.compress(raw, mtime=0)
    assert objects.get(ref) == [[0.125, -1.25], [0, 4]]
    objects.client.objects[ref.bucket, ref.key] = gzip.compress(b"[[0.125,-1.26],[0,4]]")
    with pytest.raises(PayloadUnavailable, match="integrity"):
        objects.get(ref)


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "wrong_digest", "wrong_size", "bad_version"]
)
def test_object_integrity_failures_are_explicit(objects, failure):
    ref = objects.put_verified([[0.5]])
    if failure == "missing":
        objects.client.objects.clear()
    elif failure == "corrupt":
        objects.client.objects[ref.bucket, ref.key] = b"[[0.6]]"
    elif failure == "wrong_digest":
        ref = replace(ref, sha256="0" * 64)
    elif failure == "wrong_size":
        ref = replace(ref, size=ref.size + 1)
    else:
        ref = replace(ref, version=3)
    with pytest.raises(PayloadUnavailable):
        objects.get(ref)


@pytest.mark.parametrize("failure", ["fail_put", "fail_get"])
def test_copy_failure_leaves_receipt_untouched(f, objects, failure):
    task_id, _ = receipt(f)
    original = row(task_id)
    setattr(objects.client, failure, True)
    with pytest.raises(PayloadUnavailable):
        receipt_payloads.migrate(original, objects, mode="copy")
    assert row(task_id) == original


def test_copy_compact_restore_preserve_receipt_accounting_and_replay(f, objects):
    task_id, result = receipt(f)
    original = row(task_id)
    assert receipt_payloads.migrate(original, objects, mode="copy") == "copied"
    copied = row(task_id)
    assert copied["response"]["embedding_vectors"] == result.embedding_vectors
    assert receipt_payloads.migrate(copied, objects, mode="copy") == "verified"
    assert receipt_payloads.migrate(copied, objects, mode="compact") == "compacted"
    compacted = row(task_id)
    assert "embedding_vectors" not in compacted["response"]
    assert batch_results.response_payload(compacted["response"], objects) == original["response"]
    assert {k: v for k, v in compacted.items() if k != "response"} == {
        k: v for k, v in original.items() if k != "response"
    }
    assert {k: v for k, v in compacted["response"].items() if k != "embedding_vectors_ref"} == {
        k: v for k, v in original["response"].items() if k != "embedding_vectors"
    }
    objects.client.fail_get = True
    assert batch_results.unconsumed(task_id) == []
    with batch_results.consume_result(task_id, result) as acknowledgement:
        assert not acknowledgement.pending
    with pytest.raises(PayloadUnavailable):
        receipt_payloads.migrate(compacted, objects, mode="restore")
    assert row(task_id) == compacted
    objects.client.fail_get = False
    assert receipt_payloads.migrate(compacted, objects, mode="restore") == "restored"
    assert row(task_id) == original


@pytest.mark.parametrize(
    "status,kind,consumed",
    [
        ("running", "embed_postings_batch", True),
        ("pending", "embed_postings_batch", True),
        ("done", "embed_postings_batch", False),
        ("done", "classify_job_profiles", True),
    ],
)
def test_ineligible_receipts_never_copied_or_compacted(f, objects, status, kind, consumed):
    task_id, _ = receipt(f, status=status, kind=kind, consumed=consumed)
    original = row(task_id)
    assert receipt_payloads.candidates(after=None, limit=10, mode="copy") == []
    assert receipt_payloads.migrate(original, objects, mode="copy") == "changed"
    assert row(task_id) == original
    assert objects.client.objects == {}


@pytest.mark.parametrize("change", ["status", "response", "consumed", "outcome"])
def test_concurrent_change_prevents_reference_write(f, objects, change):
    task_id, _ = receipt(f)
    original = row(task_id)

    def change_source():
        if change == "status":
            db.execute("UPDATE tasks SET status='running' WHERE id=%s", (task_id,))
        elif change == "response":
            db.execute(
                'UPDATE batch_result_receipts SET response=response || \'{"error":"changed"}\'::jsonb WHERE task_id=%s',
                (task_id,),
            )
        elif change == "consumed":
            db.execute(
                "UPDATE batch_result_receipts SET consumed_at=NULL WHERE task_id=%s", (task_id,)
            )
        else:
            db.execute("UPDATE batch_result_receipts SET outcome=NULL WHERE task_id=%s", (task_id,))

    objects.client.after_put = change_source
    assert receipt_payloads.migrate(original, objects, mode="copy") == "changed"
    assert "embedding_vectors_ref" not in row(task_id)["response"]
    assert "embedding_vectors" in row(task_id)["response"]


def test_orphan_upload_retry_and_unverified_compaction(f, objects):
    task_id, _ = receipt(f)
    original = row(task_id)
    objects.put_verified(original["response"]["embedding_vectors"])
    assert receipt_payloads.migrate(original, objects, mode="copy") == "copied"
    assert len(objects.client.objects) == 1
    copied = row(task_id)
    objects.client.fail_get = True
    with pytest.raises(PayloadUnavailable):
        receipt_payloads.migrate(copied, objects, mode="compact")
    assert row(task_id) == copied


def test_required_external_vectors_fail_explicitly_and_inline_needs_no_store(f, objects):
    task_id, _ = receipt(f, consumed=False)
    original = row(task_id)["response"]
    assert batch_results.response_payload(original) == original
    ref = objects.put_verified(original["embedding_vectors"])
    external = {**original, "embedding_vectors_ref": asdict(ref)}
    assert batch_results.response_payload(external) == original
    external.pop("embedding_vectors")
    db.execute(
        "UPDATE batch_result_receipts SET response=%s WHERE task_id=%s",
        (db.jsonb(external), task_id),
    )
    objects.client.objects.clear()
    with pytest.raises(PayloadUnavailable):
        batch_results.response_payload(external, objects)
    assert row(task_id)["outcome"] is None


def test_keyset_iteration_and_restore_selection(f, objects):
    first, _ = receipt(f)
    second, _ = receipt(f)
    page = receipt_payloads.candidates(after=None, limit=1, mode="copy")
    assert len(page) == 1
    cursor = (page[0]["provider_batch_id"], page[0]["custom_id"])
    other = receipt_payloads.candidates(after=cursor, limit=1, mode="copy")
    assert len(other) == 1 and other[0]["task_id"] != page[0]["task_id"]
    assert {page[0]["task_id"], other[0]["task_id"]} == {first, second}
    assert receipt_payloads.candidates(after=None, limit=10, mode="restore") == []


def test_reference_parse_rejects_wrong_types():
    with pytest.raises(PayloadUnavailable):
        PayloadRef.parse({"bucket": "b", "key": "k", "sha256": "x", "size": True, "version": 1})


def test_migration_rejects_outer_transaction(f, objects):
    task_id, _ = receipt(f)
    with db.transaction(), pytest.raises(RuntimeError, match="inside a database transaction"):
        receipt_payloads.migrate(row(task_id), objects, mode="copy")
    assert objects.client.objects == {}


def test_compaction_cli_requires_backup_confirmation(monkeypatch):
    from api.ai.migrate_receipt_payloads import main

    monkeypatch.setattr("sys.argv", ["migration", "compact", "--limit", "1"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_store_rejects_empty_credentials_before_sdk_discovery(monkeypatch):
    for name in ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY"):
        monkeypatch.setenv("JOBTRACKER_S3_" + name, "")
    with pytest.raises(PayloadUnavailable, match="configuration"):
        PayloadStore.from_env()


def test_compaction_rechecks_task_after_object_download(f, objects, monkeypatch):
    task_id, _ = receipt(f)
    receipt_payloads.migrate(row(task_id), objects, mode="copy")
    copied = row(task_id)
    original_get = objects.get

    def reactivate(ref):
        vectors = original_get(ref)
        db.execute("UPDATE tasks SET status='running' WHERE id=%s", (task_id,))
        return vectors

    monkeypatch.setattr(objects, "get", reactivate)
    assert receipt_payloads.migrate(copied, objects, mode="compact") == "changed"
    assert row(task_id) == copied


def test_required_receipt_loading_raises_without_acknowledging(f, objects, monkeypatch):
    task_id, result = receipt(f, consumed=False)
    original = row(task_id)["response"]
    ref = objects.put_verified(original.pop("embedding_vectors"))
    original["embedding_vectors_ref"] = asdict(ref)
    db.execute(
        "UPDATE batch_result_receipts SET response=%s WHERE task_id=%s",
        (db.jsonb(original), task_id),
    )
    monkeypatch.setattr(PayloadStore, "from_env", lambda: objects)
    assert batch_results.unconsumed(task_id)[0].embedding_vectors == result.embedding_vectors
    objects.client.fail_get = True
    with pytest.raises(PayloadUnavailable):
        batch_results.unconsumed(task_id)
    assert row(task_id)["outcome"] is None
    assert row(task_id)["consumed_at"] is None


def test_verification_works_in_read_only_session(f, objects, monkeypatch):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    import core.pool as connections

    task_id, _ = receipt(f)
    receipt_payloads.migrate(row(task_id), objects, mode="copy")
    original = row(task_id)
    # A separate read-only pool cannot leak session defaults into later tests.
    with (
        ConnectionPool(
            connections.DATABASE_URL,
            min_size=1,
            max_size=1,
            kwargs={"row_factory": dict_row, "options": "-c default_transaction_read_only=on"},
        ) as readonly,
        monkeypatch.context() as patch,
    ):
        patch.setattr(connections, "pool", readonly)
        assert db.query_one("SHOW default_transaction_read_only") == {
            "default_transaction_read_only": "on"
        }
        assert receipt_payloads.migrate(original, objects, mode="verify") == "verified"
    assert row(task_id) == original
