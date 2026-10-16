import json

import pytest

from homing.session import EncryptedFileSessionStore, generate_key

pytestmark = pytest.mark.unit

GOOD = {
    "cookies": [{"name": "s", "value": "v", "domain": "x.test", "path": "/"}],
    "origins": [],
}


@pytest.fixture
def store(tmp_path):
    return EncryptedFileSessionStore(tmp_path, generate_key())


def test_round_trip_is_encrypted_at_rest(store, tmp_path):
    store.save("acme", "default", GOOD)
    raw = store.path("acme", "default").read_bytes()
    assert b"cookies" not in raw
    assert store.load("acme", "default").state == GOOD
    assert not list(tmp_path.glob("*.tmp"))


def test_missing(store):
    assert store.load("acme", "default").reason == "missing"


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"", "undecryptable"), (b"garbage!!", "undecryptable")],
)
def test_corrupt_files(store, content, reason):
    store.path("acme", "default").write_bytes(content)
    result = store.load("acme", "default")
    assert result.state is None
    assert result.reason == reason


def test_wrong_key(store, tmp_path):
    store.save("acme", "default", GOOD)
    other = EncryptedFileSessionStore(tmp_path, generate_key())
    assert other.load("acme", "default").reason == "undecryptable"


def test_bad_json_and_bad_structure(store):
    path = store.path("acme", "default")
    path.write_bytes(store._fernet.encrypt(b"{not json"))
    assert store.load("acme", "default").reason == "bad_json"
    path.write_bytes(
        store._fernet.encrypt(json.dumps({"cookies": "nope"}).encode())
    )
    assert store.load("acme", "default").reason == "bad_structure"


def test_sessions_are_keyed_by_credential(store):
    store.save("acme", "clinic-a", GOOD)
    assert store.load("acme", "clinic-b").reason == "missing"
