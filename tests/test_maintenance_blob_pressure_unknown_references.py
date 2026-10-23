from easychange.core.maintenance_service import MaintenanceService


def test_pressure_cleanup_without_reference_snapshot_preserves_journal_blobs(tmp_path):
    service = MaintenanceService(tmp_path)
    blobs = service.easy_root / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / ("a" * 64 + ".zlib")
    blob.write_bytes(b"preserve-without-reference-snapshot")
    service.max_state_bytes = 1

    report = service.automatic_cleanup(referenced_blobs=None)

    assert blob.exists(), "Unknown journal references must not be treated as orphaned"
    assert "orphan_blobs_pressure" not in report["categories"]


def test_pressure_cleanup_with_explicit_empty_snapshot_removes_orphan(tmp_path):
    service = MaintenanceService(tmp_path)
    blobs = service.easy_root / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / ("b" * 64 + ".zlib")
    blob.write_bytes(b"unreferenced")
    service.max_state_bytes = 1

    service.automatic_cleanup(referenced_blobs=set())

    assert not blob.exists()
