"""Entity keyfiles: a local identity that survives the process (design D1).

Milestone 3's advert stubs generate a keypair per process, which is right for a
receive-only dry run and wrong the moment another node stores our public key: a
peer whose contact list is full of one-run identities makes an exercise
unrepeatable. This module is the deliberate, minimal exception to "no
persistence before milestone 5" — one JSON file per entity, holding the 32-byte
seed and nothing a database would give us.

It lives outside `protocol/` on purpose. `protocol/` is pure functions over
bytes with no I/O (DESIGN.md §11, enforced by
`tests/protocol/test_import_boundary.py`), and reading a file is I/O; the
seed → identity step stays in `protocol/identity.py`, where it already was.

The file format is deliberately boring, because milestone 5 has to import it in
one function and then encrypt it at rest per §6:

    {
      "version": 1,
      "name": "sighop-test",
      "node_type": 1,
      "seed_hex": "<64 hex chars>",
      "public_key_hex": "<64 hex chars>",
      "created_at": "2026-09-05T12:00:00+00:00",
      "burned": false
    }

`burned` marks a keypair that has been published — committed to this repository
as a test vector (design D11). It is not a real identity and must never be used
on air; the marker travels in the file so the fact cannot be lost by copying it
somewhere else.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import structlog

from sighop.logging import get_logger
from sighop.protocol.identity import (
    PUB_KEY_SIZE,
    SEED_SIZE,
    LocalIdentity,
    generate_identity,
)
from sighop.protocol.payloads import NodeType

KEYFILE_VERSION = 1

KEYFILE_MODE = 0o600
"""Owner read/write and nothing else. Applied at creation, not afterwards."""

BURNED_WARNING = (
    "this keypair has been published as a test vector and must never be used "
    "on air; generate a new one with `sighop keys new`"
)


class KeyfileError(RuntimeError):
    """A keyfile could not be created or loaded. Always names the path."""


class KeyfileExistsError(KeyfileError):
    """The target path already holds a key, and identities are not overwritten."""


class KeyfileMismatchError(KeyfileError):
    """The stored public key is not the one the stored seed derives."""


class NodeHashCollisionError(KeyfileError):
    """Two local entities share a node hash, which DESIGN.md §3 rule 3 forbids."""


@dataclass(frozen=True, slots=True)
class Keyfile:
    """A loaded entity identity, with the file it came from.

    `warnings` carries what was wrong but not fatal — broad permissions, a
    burned key — so the command line can show it and a test can assert it,
    rather than the fact existing only in a log stream nobody reads.
    """

    path: Path
    version: int
    name: str
    node_type: NodeType | int
    identity: LocalIdentity
    created_at: str = ""
    burned: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def public_key(self) -> bytes:
        return self.identity.public_key

    @property
    def node_hash(self) -> int:
        return self.identity.node_hash

    def as_json(self) -> dict[str, object]:
        """What an identity is, without what makes it secret."""
        return {
            "keyfile": str(self.path),
            "entity_name": self.name,
            "node_type": int(self.node_type),
            "public_key": self.public_key.hex(),
            "node_hash": self.node_hash,
            "burned": self.burned,
        }


def keyfile_document(
    identity: LocalIdentity,
    name: str,
    node_type: NodeType | int,
    *,
    burned: bool = False,
    created_at: str | None = None,
) -> dict[str, object]:
    """The JSON document a keyfile holds. The whole format lives here."""
    document: dict[str, object] = {
        "version": KEYFILE_VERSION,
        "name": name,
        "node_type": int(node_type),
        "seed_hex": identity.seed.hex(),
        "public_key_hex": identity.public_key.hex(),
        "created_at": created_at or dt.datetime.now(dt.UTC).isoformat(),
        "burned": burned,
    }
    if burned:
        document["burned_warning"] = BURNED_WARNING
    return document


def identity_from_document(document: dict, path: Path | str = "<memory>") -> LocalIdentity:
    """The seed → identity step, with the stored public key checked against it.

    Milestone 5 imports a keyfile through this function: it is the one place
    that turns the stored format into a `LocalIdentity`, and the one place the
    mismatch check happens. A mismatch is an error naming the file rather than
    a silently preferred value — either half could be the corrupted one, and
    guessing which would produce an identity nobody holds.
    """
    try:
        seed = bytes.fromhex(str(document["seed_hex"]))
        stored_public_key = bytes.fromhex(str(document["public_key_hex"]))
    except KeyError as exc:
        raise KeyfileError(f"{path}: keyfile is missing the {exc} field") from exc
    except ValueError as exc:
        raise KeyfileError(f"{path}: keyfile holds a field that is not hex: {exc}") from exc

    if len(seed) != SEED_SIZE:
        raise KeyfileError(f"{path}: seed is {len(seed)} bytes, expected {SEED_SIZE}")
    if len(stored_public_key) != PUB_KEY_SIZE:
        raise KeyfileError(
            f"{path}: public key is {len(stored_public_key)} bytes, expected {PUB_KEY_SIZE}"
        )

    identity = LocalIdentity.from_seed(seed)
    if identity.public_key != stored_public_key:
        raise KeyfileMismatchError(
            f"{path}: the stored public key {stored_public_key.hex()} is not the one "
            f"the stored seed derives ({identity.public_key.hex()}); refusing to "
            "prefer either value"
        )
    return identity


def create_keyfile(
    path: Path,
    name: str,
    *,
    node_type: NodeType | int = NodeType.CHAT,
    avoid_node_hashes: frozenset[int] = frozenset(),
    identity: LocalIdentity | None = None,
    burned: bool = False,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> Keyfile:
    """Generate an identity and write it, refusing to touch an existing file.

    The file is created with `O_EXCL` and mode `0600` in one call: the refusal
    to overwrite and the permission bits are both properties of the open, so
    neither depends on a check-then-write window or on the process umask.
    """
    log = logger or get_logger(component="keystore")
    if identity is None:
        identity = generate_identity(avoid_node_hashes=avoid_node_hashes)
    elif identity.node_hash in avoid_node_hashes:
        raise NodeHashCollisionError(
            f"{path}: node hash 0x{identity.node_hash:02x} is already taken by "
            "another local entity"
        )

    document = keyfile_document(identity, name, node_type, burned=burned)
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, KEYFILE_MODE
        )
    except FileExistsError as exc:
        raise KeyfileExistsError(
            f"{path}: a keyfile already exists here and will not be overwritten; "
            "another node may already hold that identity"
        ) from exc
    except OSError as exc:
        raise KeyfileError(f"{path}: could not create the keyfile: {exc}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")

    keyfile = Keyfile(
        path=Path(path),
        version=KEYFILE_VERSION,
        name=name,
        node_type=node_type,
        identity=identity,
        created_at=str(document["created_at"]),
        burned=burned,
        warnings=(BURNED_WARNING,) if burned else (),
    )
    log.info("keyfile_created", **keyfile.as_json())
    return keyfile


def _permission_warning(path: Path) -> str | None:
    """A warning when anyone but the owner can reach the seed."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:  # pragma: no cover - the read below would have failed first
        return None
    if mode & 0o077:
        return (
            f"{path} is mode {mode:04o}, which is broader than owner-only "
            f"({KEYFILE_MODE:04o}); the seed is readable by other users"
        )
    return None


def load_keyfile(
    path: Path, *, logger: structlog.stdlib.BoundLogger | None = None
) -> Keyfile:
    """Load an identity from a keyfile, checking what the file claims."""
    log = logger or get_logger(component="keystore")
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyfileError(f"{path}: could not read the keyfile: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyfileError(f"{path}: keyfile is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise KeyfileError(f"{path}: keyfile is not a JSON object")

    version = int(document.get("version", 0))
    if version != KEYFILE_VERSION:
        raise KeyfileError(
            f"{path}: keyfile version {version} is not the supported version "
            f"{KEYFILE_VERSION}"
        )

    identity = identity_from_document(document, path)
    node_type_value = int(document.get("node_type", int(NodeType.CHAT)))
    try:
        node_type: NodeType | int = NodeType(node_type_value)
    except ValueError:
        node_type = node_type_value

    warnings: list[str] = []
    if (warning := _permission_warning(Path(path))) is not None:
        warnings.append(warning)
        log.error("keyfile_permissions_broad", keyfile=str(path), detail=warning)
    burned = bool(document.get("burned", False))
    if burned:
        warnings.append(BURNED_WARNING)
        log.error("keyfile_burned", keyfile=str(path), detail=BURNED_WARNING)

    keyfile = Keyfile(
        path=Path(path),
        version=version,
        name=str(document.get("name", "")),
        node_type=node_type,
        identity=identity,
        created_at=str(document.get("created_at", "")),
        burned=burned,
        warnings=tuple(warnings),
    )
    log.info("keyfile_loaded", **keyfile.as_json())
    return keyfile


class EntityRegistry:
    """The local entities one run holds, with the §3 rule 3 check between them.

    Two local entities sharing a node hash would make an inbound packet
    ambiguous between our *own* identities, which is the one collision case the
    design rules out rather than handles. Generation is fed the hashes already
    taken so a new key avoids them; loading two files that collide is a startup
    failure naming both.
    """

    def __init__(self, *, logger: structlog.stdlib.BoundLogger | None = None) -> None:
        self._entities: list[Keyfile] = []
        self._log = logger or get_logger(component="keystore")

    @property
    def entities(self) -> tuple[Keyfile, ...]:
        return tuple(self._entities)

    def __len__(self) -> int:
        return len(self._entities)

    def taken_node_hashes(self) -> frozenset[int]:
        return frozenset(entity.node_hash for entity in self._entities)

    def load(self, path: Path) -> Keyfile:
        return self._register(load_keyfile(path, logger=self._log))

    def create(
        self,
        path: Path,
        name: str,
        *,
        node_type: NodeType | int = NodeType.CHAT,
        burned: bool = False,
    ) -> Keyfile:
        return self._register(
            create_keyfile(
                path,
                name,
                node_type=node_type,
                avoid_node_hashes=self.taken_node_hashes(),
                burned=burned,
                logger=self._log,
            )
        )

    def _register(self, keyfile: Keyfile) -> Keyfile:
        for existing in self._entities:
            if existing.node_hash == keyfile.node_hash:
                raise NodeHashCollisionError(
                    f"local entities {existing.path} and {keyfile.path} share node "
                    f"hash 0x{keyfile.node_hash:02x}; two local entities with one "
                    "node hash make an inbound packet ambiguous between our own "
                    "identities (DESIGN.md §3)"
                )
        self._entities.append(keyfile)
        return keyfile
