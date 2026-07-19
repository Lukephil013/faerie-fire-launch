"""Portable recovery envelope and automatic-key portability tests."""
from __future__ import annotations

import base64
import gzip
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc import recovery


@pytest.fixture(autouse=True)
def _clean_key_mode(monkeypatch):
    for name in ("LIVINGPC_DB_KEY", "LIVINGPC_KEY_FILE", "LIVINGPC_SALT_FILE",
                 "LIVINGPC_AUTO_ENCRYPTION"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def mock_dpapi(monkeypatch):
    prefix = b"mock-dpapi-v1:"

    def protect(value: bytes) -> bytes:
        return prefix + value[::-1]

    def unprotect(value: bytes) -> bytes:
        if not value.startswith(prefix):
            raise recovery.crypto.EncryptionError("invalid mock envelope")
        return value[len(prefix):][::-1]

    monkeypatch.setattr(recovery.crypto, "dpapi_available", lambda: True)
    monkeypatch.setattr(recovery.crypto, "protect_secret", protect)
    monkeypatch.setattr(recovery.crypto, "unprotect_secret", unprotect)
    return protect, unprotect


@pytest.fixture
def repository(tmp_path, mock_dpapi):
    key_path = tmp_path / "repository.key"
    material = recovery.create_repository_key(
        "correct horse battery staple", str(key_path), _scrypt_n=2 ** 10)
    return key_path, material


def _make_source(root: Path) -> Path:
    source = root / "source"
    (source / "projects" / "empty").mkdir(parents=True)
    (source / "projects" / "plan.md").write_text(
        "private launch plan\n", encoding="utf-8")
    (source / "portrait.json").write_text(
        '{"language":"en","name":"Faerie"}', encoding="utf-8")
    (source / "café.txt").write_bytes(b"unicode path")
    return source


def _create_bundle(tmp_path: Path, material: recovery.RepositoryKeyMaterial):
    source = _make_source(tmp_path)
    bundle = tmp_path / "faerie-fire-test.ffbackup"
    info = recovery.encrypt_bundle(
        str(source), str(bundle), material.key, material.passphrase_wrapper,
        {"privacy_epoch": 7, "app_version": "test", "reason": "manual"},
    )
    return source, bundle, info


def test_repository_key_is_dpapi_cached_and_loadable(tmp_path, mock_dpapi):
    key_path = tmp_path / "repository.key"
    material = recovery.create_repository_key(
        "recovery phrase", str(key_path), _scrypt_n=2 ** 10)
    encoded = key_path.read_bytes()

    assert material.key not in encoded
    assert b"recovery phrase" not in encoded
    loaded = recovery.load_repository_key(str(key_path))
    assert loaded.key == material.key
    assert loaded.passphrase_wrapper == material.passphrase_wrapper
    assert repr(material).find(material.key.hex()) == -1


def test_bundle_roundtrip_and_public_inspection(tmp_path, repository):
    _, material = repository
    source, bundle, created = _create_bundle(tmp_path, material)

    inspected = recovery.inspect_bundle(str(bundle))
    assert recovery.verify_bundle(str(bundle), material.key).bundle_id == inspected.bundle_id
    assert inspected.bundle_id == created.bundle_id
    assert inspected.privacy_epoch == 7
    assert inspected.header_meta == {"app_version": "test", "reason": "manual"}
    assert inspected.source_size == sum(
        path.stat().st_size for path in source.rglob("*") if path.is_file())
    assert inspected.size_bytes == bundle.stat().st_size
    assert inspected.wrapper.key_id == material.passphrase_wrapper.key_id
    raw_bundle = bundle.read_bytes()
    assert b"private launch plan" not in raw_bundle
    assert b"portrait.json" not in raw_bundle

    destination = tmp_path / "restored"
    restored_info = recovery.decrypt_bundle(
        str(bundle), "correct horse battery staple", str(destination))
    assert restored_info.bundle_id == inspected.bundle_id
    assert (destination / "projects" / "plan.md").read_bytes() == (
        source / "projects" / "plan.md").read_bytes()
    assert (destination / "portrait.json").read_bytes() == (
        source / "portrait.json").read_bytes()
    assert (destination / "café.txt").read_bytes() == b"unicode path"
    assert (destination / "projects" / "empty").is_dir()


def test_wrong_passphrase_leaves_no_destination(tmp_path, repository):
    _, material = repository
    _, bundle, _ = _create_bundle(tmp_path, material)
    destination = tmp_path / "must-not-exist"

    with pytest.raises(recovery.BackupAuthenticationError):
        recovery.decrypt_bundle(str(bundle), "wrong passphrase", str(destination))
    assert not destination.exists()


def test_tampered_ciphertext_is_rejected_without_partial_restore(tmp_path, repository):
    _, material = repository
    _, bundle, _ = _create_bundle(tmp_path, material)
    tampered = tmp_path / "tampered.ffbackup"
    data = bytearray(bundle.read_bytes())
    data[-recovery.GCM_TAG_SIZE - 2] ^= 0x40
    tampered.write_bytes(data)
    destination = tmp_path / "tampered-restore"

    # Public metadata is still parseable; authenticated restore must fail.
    assert recovery.inspect_bundle(str(tampered)).bundle_id
    with pytest.raises(recovery.BackupAuthenticationError):
        recovery.verify_bundle(str(tampered), material.key)
    with pytest.raises(recovery.BackupAuthenticationError):
        recovery.decrypt_bundle(
            str(tampered), "correct horse battery staple", str(destination))
    assert not destination.exists()


def test_truncated_envelope_is_rejected(tmp_path):
    bundle = tmp_path / "truncated.ffbackup"
    bundle.write_bytes(recovery.MAGIC + b"\x01")
    with pytest.raises(recovery.BackupFormatError, match="truncated"):
        recovery.inspect_bundle(str(bundle))


def test_authenticated_traversal_member_is_rejected(tmp_path, repository,
                                                     monkeypatch):
    _, material = repository
    source = tmp_path / "source"
    source.mkdir()
    (source / "one.txt").write_bytes(b"x")

    def malicious_payload(entries, sink):
        path = b"../escaped.txt"
        with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as archive:
            archive.write(recovery.PAYLOAD_MAGIC)
            archive.write(recovery._ENTRY.pack(
                recovery._ENTRY_FILE, len(path), 1, 0, stat.S_IRUSR))
            archive.write(path)
            archive.write(b"x")
            archive.write(recovery._ENTRY.pack(recovery._ENTRY_END, 0, 0, 0, 0))

    monkeypatch.setattr(recovery, "_write_payload", malicious_payload)
    bundle = tmp_path / "traversal.ffbackup"
    with pytest.raises(recovery.UnsafePathError):
        recovery.encrypt_bundle(str(source), str(bundle), material.key,
                                material.passphrase_wrapper)
    assert not bundle.exists()
    assert not any(path.name.endswith(".partial") for path in tmp_path.iterdir())
    assert not (tmp_path / "escaped.txt").exists()


def test_source_links_are_rejected_when_supported(tmp_path, repository):
    _, material = repository
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available to this Windows user")

    with pytest.raises(recovery.UnsafePathError, match="links|reparse"):
        recovery.encrypt_bundle(
            str(source), str(tmp_path / "link.ffbackup"), material.key,
            material.passphrase_wrapper)


def test_database_key_export_encode_and_destination_rewrap(tmp_path, mock_dpapi):
    protect, _ = mock_dpapi
    secret = bytes(range(32))
    salt_file_bytes = base64.b64encode(b"s" * 16) + b"\n"
    source_key = tmp_path / "old" / "secret.key"
    source_salt = tmp_path / "old" / "secret.salt"
    source_key.parent.mkdir()
    source_key.write_bytes(protect(secret))
    source_salt.write_bytes(salt_file_bytes)

    material = recovery.export_automatic_database_key(
        key_path=str(source_key), salt_file=str(source_salt))
    assert material.secret == secret
    assert material.salt_file == salt_file_bytes
    assert secret.hex() not in repr(material)

    encoded = recovery.encode_database_key_material(material)
    decoded = recovery.decode_database_key_material(encoded)
    assert decoded == material

    destination_key = tmp_path / "new" / "secret.key"
    destination_salt = tmp_path / "new" / "secret.salt"
    recovery.install_automatic_database_key(
        decoded, key_path=str(destination_key), salt_file=str(destination_salt))
    assert destination_key.read_bytes() == protect(secret)
    assert destination_salt.read_bytes() == salt_file_bytes


def test_database_key_export_never_generates_missing_files(tmp_path, mock_dpapi):
    key = tmp_path / "missing" / "secret.key"
    salt = tmp_path / "missing" / "secret.salt"
    with pytest.raises(FileNotFoundError):
        recovery.export_automatic_database_key(
            key_path=str(key), salt_file=str(salt))
    assert not key.exists()
    assert not salt.exists()


@pytest.mark.parametrize("environment_name", [
    "LIVINGPC_DB_KEY", "LIVINGPC_KEY_FILE", "LIVINGPC_SALT_FILE",
])
def test_database_key_portability_refuses_custom_key_modes(
        tmp_path, mock_dpapi, monkeypatch, environment_name):
    monkeypatch.setenv(environment_name, "configured")
    with pytest.raises(recovery.RecoveryUnsupportedError, match="automatic"):
        recovery.export_automatic_database_key(
            key_path=str(tmp_path / "secret.key"),
            salt_file=str(tmp_path / "secret.salt"))


def test_database_key_portability_refuses_disabled_auto_encryption(
        tmp_path, mock_dpapi, monkeypatch):
    monkeypatch.setenv("LIVINGPC_AUTO_ENCRYPTION", "false")
    with pytest.raises(recovery.RecoveryUnsupportedError, match="automatic"):
        recovery.export_automatic_database_key(
            key_path=str(tmp_path / "secret.key"),
            salt_file=str(tmp_path / "secret.salt"))
