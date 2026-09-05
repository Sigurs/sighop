"""Entity keyfiles (milestone 4, `local-identity`, design D1).

A keyfile is the one thing this milestone writes to disk, and every property
tested here is a property of *not* losing or leaking an identity: the file is
never silently overwritten, its permissions are owner-only from the moment it
exists, its two halves are checked against each other rather than one being
preferred, and two local entities can never share a node hash.
"""

from __future__ import annotations

import json
import stat

import pytest

from sighop.keystore import (
    BURNED_WARNING,
    KEYFILE_MODE,
    EntityRegistry,
    KeyfileError,
    KeyfileExistsError,
    KeyfileMismatchError,
    NodeHashCollisionError,
    create_keyfile,
    keyfile_document,
    load_keyfile,
)
from sighop.protocol.identity import LocalIdentity, generate_identity
from sighop.protocol.payloads import NodeType

# --- Round trip ------------------------------------------------------------


def test_a_created_keyfile_loads_back_as_the_same_identity(tmp_path) -> None:
    path = tmp_path / "entity.json"

    created = create_keyfile(path, "skogen")
    loaded = load_keyfile(path)

    assert loaded.identity == created.identity
    assert loaded.public_key == created.public_key
    assert loaded.node_hash == created.node_hash
    assert loaded.name == "skogen"
    assert loaded.node_type is NodeType.CHAT


def test_the_document_shape_is_what_milestone_five_will_import() -> None:
    identity = generate_identity()

    document = keyfile_document(identity, "skogen", NodeType.CHAT)

    assert document["version"] == 1
    assert document["seed_hex"] == identity.seed.hex()
    assert document["public_key_hex"] == identity.public_key.hex()
    assert document["node_type"] == int(NodeType.CHAT)
    assert document["burned"] is False


# --- Never overwritten -----------------------------------------------------


def test_creation_refuses_to_write_over_an_existing_file(tmp_path) -> None:
    path = tmp_path / "entity.json"
    first = create_keyfile(path, "skogen")
    before = path.read_text()

    with pytest.raises(KeyfileExistsError, match=str(path)):
        create_keyfile(path, "annan")

    assert path.read_text() == before, "the existing identity was modified"
    assert load_keyfile(path).public_key == first.public_key


# --- Permissions -----------------------------------------------------------


def test_a_new_keyfile_is_owner_only(tmp_path) -> None:
    path = tmp_path / "entity.json"

    create_keyfile(path, "skogen")

    assert stat.S_IMODE(path.stat().st_mode) == KEYFILE_MODE


def test_loading_a_world_readable_keyfile_warns_and_still_loads(tmp_path) -> None:
    path = tmp_path / "entity.json"
    created = create_keyfile(path, "skogen")
    path.chmod(0o644)

    loaded = load_keyfile(path)

    assert loaded.public_key == created.public_key
    assert any(str(path) in warning and "0644" in warning for warning in loaded.warnings)


# --- The two halves must agree ---------------------------------------------


def test_a_seed_that_does_not_derive_the_stored_public_key_is_refused(tmp_path) -> None:
    path = tmp_path / "entity.json"
    create_keyfile(path, "skogen")
    document = json.loads(path.read_text())
    document["public_key_hex"] = generate_identity().public_key.hex()
    path.write_text(json.dumps(document))

    with pytest.raises(KeyfileMismatchError) as excinfo:
        load_keyfile(path)

    assert str(path) in str(excinfo.value)
    assert "refusing to prefer either value" in str(excinfo.value)


def test_a_keyfile_from_a_future_version_is_refused(tmp_path) -> None:
    path = tmp_path / "entity.json"
    create_keyfile(path, "skogen")
    document = json.loads(path.read_text())
    document["version"] = 99
    path.write_text(json.dumps(document))

    with pytest.raises(KeyfileError, match="version 99"):
        load_keyfile(path)


# --- Node hash collisions (§3 rule 3) --------------------------------------


def _identity_with_node_hash(node_hash: int) -> LocalIdentity:
    while True:
        candidate = generate_identity()
        if candidate.node_hash == node_hash:
            return candidate


def test_two_colliding_keyfiles_are_refused_by_name(tmp_path) -> None:
    first_identity = generate_identity()
    second_identity = _identity_with_node_hash(first_identity.node_hash)
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    create_keyfile(first, "one", identity=first_identity)
    create_keyfile(second, "two", identity=second_identity)

    registry = EntityRegistry()
    registry.load(first)
    with pytest.raises(NodeHashCollisionError) as excinfo:
        registry.load(second)

    message = str(excinfo.value)
    assert str(first) in message
    assert str(second) in message
    assert f"0x{first_identity.node_hash:02x}" in message


def test_generation_avoids_a_loaded_entity_node_hash(tmp_path) -> None:
    registry = EntityRegistry()
    first = registry.load(create_keyfile(tmp_path / "one.json", "one").path)

    for index in range(12):
        created = registry.create(tmp_path / f"gen-{index}.json", f"gen-{index}")
        assert created.node_hash != first.node_hash

    hashes = [entity.node_hash for entity in registry.entities]
    assert len(hashes) == len(set(hashes)), "the registry admitted a collision"


# --- The burned test identity (design D11) ---------------------------------


def test_a_burned_keyfile_says_so_in_the_file_and_on_load(tmp_path) -> None:
    path = tmp_path / "burned.json"

    create_keyfile(path, "burned-fixture", burned=True)

    document = json.loads(path.read_text())
    assert document["burned"] is True
    assert "never be used" in document["burned_warning"]
    assert BURNED_WARNING in load_keyfile(path).warnings


# --- The seed is not in the inspection view --------------------------------


def test_the_json_view_of_an_identity_carries_no_seed(tmp_path) -> None:
    keyfile = create_keyfile(tmp_path / "entity.json", "skogen")

    rendered = json.dumps(keyfile.as_json())

    assert keyfile.public_key.hex() in rendered
    assert keyfile.identity.seed.hex() not in rendered
