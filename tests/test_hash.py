"""Pin the wrangler-compatible hash algorithm with fixed values.

If either test ever fails after a dependency bump, the content-addressing
no longer matches wrangler's and deploys would re-upload (or worse, collide).
"""

from cf_publish.pages import file_hash


def test_known_value():
    # blake3(base64(b"hello cloudflare pages") + "txt")[:32]
    assert file_hash(b"hello cloudflare pages", "txt") == "d67502feeeaca63856923e6835f00ba3"


def test_suffix_changes_hash():
    assert file_hash(b"same body", "html") != file_hash(b"same body", "js")


def test_length_is_32_hex():
    h = file_hash(b"", "")
    assert len(h) == 32
    int(h, 16)  # raises if not hex
