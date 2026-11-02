"""Upload sandboxing: filenames, magic-byte typing, size and quota limits, base64, SSRF guards."""

import base64

import pytest

from scholarocr.exceptions import AccessDeniedError, InputError, LimitExceededError, UnsupportedFormatError
from scholarocr.remote.sandbox import HttpFetcher, UserSandbox, decode_base64, safe_filename

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.mark.parametrize("raw, expected", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\Windows\\System32\\cmd.exe", "cmd.exe"),
    ("C:\\Users\\x\\scan.PDF", "scan.pdf"),
    ("/abs/path/notes.png", "notes.png"),
    ("a\x00b.pdf", "ab.pdf"),
    ("..", "upload"),
    ("", "upload"),
    (None, "upload"),
    ("we|ird<>:name?.jpg", "we_ird___name_.jpg"),
])
def test_safe_filename_strips_paths(raw, expected):
    assert safe_filename(raw) == expected


def test_user_id_must_be_well_formed(remote_cfg):
    for bad in ("../escape", "u_../../x", "C:\\temp", "", "U_ABC"):
        with pytest.raises(AccessDeniedError):
            UserSandbox(remote_cfg, bad)


def test_stage_writes_only_inside_user_inbox(remote_cfg, corpus):
    sb = UserSandbox(remote_cfg, "u_" + "a" * 24)
    batch = sb.new_batch()
    staged = sb.stage(batch, corpus["clean_digital.pdf"].read_bytes(), "../../../../evil.pdf", 0)
    assert staged.path.resolve().is_relative_to(sb.inbox)
    assert staged.media_type == "application/pdf" and staged.filename == "evil.pdf"
    # a claimed name never decides the type: PNG bytes named .pdf are stored as .png
    png = sb.stage(batch, PNG, "fake.pdf", 1)
    assert png.media_type == "image/png" and png.path.suffix == ".png"
    with pytest.raises(AccessDeniedError):
        sb.stage(sb.inbox.parent / "data", PNG, "x.png", 2)
    UserSandbox.discard(batch)
    assert not batch.exists()


def test_stage_rejects_non_documents_and_disallowed_types(remote_cfg):
    sb = UserSandbox(remote_cfg, "u_" + "b" * 24)
    batch = sb.new_batch()
    for data in (b"MZ\x90\x00 windows executable", b"#!/bin/sh\nrm -rf /\n", b"<html><script>"):
        with pytest.raises(UnsupportedFormatError):
            sb.stage(batch, data, "doc.pdf", 0)
    with pytest.raises(InputError):
        sb.stage(batch, b"", "empty.png", 0)
    # DOCX is off by default
    with pytest.raises(UnsupportedFormatError):
        sb.stage(batch, b"PK\x03\x04" + b"\x00" * 40, "x.docx", 0)
    assert [p.name for p in batch.iterdir()] == []


def test_size_and_quota_limits(remote_cfg):
    cfg = remote_cfg.model_copy(update={"max_file_mb": 0.001, "user_quota_mb": 0.002})
    sb = UserSandbox(cfg, "u_" + "c" * 24)
    batch = sb.new_batch()
    with pytest.raises(LimitExceededError):
        sb.stage(batch, PNG + b"\x00" * 2000, "big.png", 0)
    sb.stage(batch, PNG + b"\x00" * 900, "ok1.png", 0)
    sb.stage(batch, PNG + b"\x00" * 900, "ok2.png", 1)
    with pytest.raises(LimitExceededError, match="quota"):
        sb.stage(batch, PNG + b"\x00" * 900, "ok3.png", 2)


def test_base64_decoding_limits():
    raw = PNG * 3
    assert decode_base64(base64.b64encode(raw).decode(), 10_000) == raw
    assert decode_base64("data:image/png;base64," + base64.b64encode(raw).decode(), 10_000) == raw
    with pytest.raises(LimitExceededError):
        decode_base64(base64.b64encode(b"\x00" * 5000).decode(), 1000)
    with pytest.raises(InputError):
        decode_base64("not base64 !!!", 10_000)
    with pytest.raises(InputError):
        decode_base64("", 10_000)


@pytest.mark.parametrize("url", [
    "http://example.com/file.pdf",  # not https
    "file:///etc/passwd",
    "https://127.0.0.1/x.pdf",
    "https://10.0.0.5/x.pdf",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/x.pdf",
    "ftp://example.com/x.pdf",
])
def test_fetcher_blocks_ssrf_targets(url):
    with pytest.raises(AccessDeniedError):
        HttpFetcher().fetch(url, max_bytes=1000, timeout_s=1)


def test_fetcher_host_allowlist():
    with pytest.raises(AccessDeniedError, match="DOWNLOAD_HOSTS"):
        HttpFetcher(["files.oaiusercontent.com"]).fetch("https://evil.example/x.pdf", max_bytes=10, timeout_s=1)
