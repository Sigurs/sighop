"""Entity keyfiles: a local identity that survives the process (design D1).

Milestone 3's advert stubs generate a keypair per process, which is right for a
receive-only dry run and wrong the moment another node stores our public key: a
peer whose contact list is full of one-run identities makes an exercise
unrepeatable. This module is the deliberate, minimal exception to "no
persistence before milestone 5" — one JSON file per entity, holding the 64-byte
private key and nothing a database would give us.

It lives outside `protocol/` on purpose. `protocol/` is pure functions over
bytes with no I/O (DESIGN.md §11, enforced by
`tests/protocol/test_import_boundary.py`), and reading a file is I/O; the
document → identity step stays in `protocol/identity.py`, where it already was.

The file format is deliberately boring, because milestone 5 has to import it in
one function and then encrypt it at rest per §6:

    {
      "version": 2,
      "name": "sighop-test",
      "node_type": 1,
      "private_key_hex": "<128 hex chars>",
      "public_key_hex": "<64 hex chars>",
      "created_at": "2026-09-05T12:00:00+00:00",
      "burned": false
    }

Version 1 recorded `seed_hex` instead, and is refused rather than read: a seed
is not a representation this system holds any more, and rewriting an operator's
file on their behalf is not this module's call to make (change
`create-entity-with-known-key`, design D3). The refusal says which version it
found and what to do, because "version 1 is not version 2" reads like corruption
for a file we ourselves wrote.

`burned` marks a keypair that has been published — committed to this repository
as a test vector (design D11). It is not a real identity and must never be used
on air; the marker travels in the file so the fact cannot be lost by copying it
somewhere else.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sighop.logging import Logger, get_logger
from sighop.protocol.identity import (
    PRV_KEY_SIZE,
    PUB_KEY_SIZE,
    LocalIdentity,
    generate_identity,
)
from sighop.protocol.payloads import NodeType

KEYFILE_VERSION = 2

REMOVED_SEED_VERSION = 1
"""The version that recorded a seed. Written by builds before the private key
became the one representation; read by none, and named in its own refusal."""

KEYFILE_MODE = 0o600
"""Owner read/write and nothing else. Applied at creation, not afterwards."""

BURNED_WARNING = (
    "this keypair has been published as a test vector and must never be used "
    "on air; generate a new one with `sighop keys new`"
)

PLAINTEXT_KEY_NOTICE = (
    "this file holds an UNENCRYPTED 64-byte private key, protected only by its "
    f"filesystem permissions ({KEYFILE_MODE:04o}); the same key inside the entity "
    "store is sealed under SIGHOP_SECRET_KEY, and the two do not offer the same "
    "protection"
)
"""Printed wherever a keyfile is created or exported (milestone 5, `local-identity`).

Milestone 5 encrypts the key at rest, which makes the keyfile the *weaker* of
the two stores rather than the only one. An operator who has stopped thinking
about keyfile permissions because "sighop encrypts keys now" is the failure this
sentence exists to prevent, and it has to appear at the moment the file is
written rather than in documentation."""


class KeyfileError(RuntimeError):
    """A keyfile could not be created or loaded. Always names the path."""


class KeyfileExistsError(KeyfileError):
    """The target path already holds a key, and identities are not overwritten."""


class KeyfileMismatchError(KeyfileError):
    """The stored public key is not the one the stored private key derives."""


class KeyfileRemovedFormatError(KeyfileError):
    """The file is a version 1 keyfile, which recorded a seed.

    Its own type, because the caller may want to say something different about a
    file this system wrote under a format it has since dropped than about a file
    it cannot make sense of at all.
    """


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
        "private_key_hex": identity.private_key.hex(),
        "public_key_hex": identity.public_key.hex(),
        "created_at": created_at or dt.datetime.now(dt.UTC).isoformat(),
        "burned": burned,
    }
    if burned:
        document["burned_warning"] = BURNED_WARNING
    return document


def keyfile_bytes(document: dict[str, object]) -> bytes:
    """A keyfile document as the bytes a keyfile holds.

    One serialiser, because milestone 8's panel hands the same document to a
    browser as a download: a file written here and a file saved from there are
    interchangeable only if nothing decides separately how to spell them.
    """
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def identity_from_document(document: dict, path: Path | str = "<memory>") -> LocalIdentity:
    """The document → identity step, with the stored public key checked against it.

    Milestone 5 imports a keyfile through this function: it is the one place
    that turns the stored format into a `LocalIdentity`, and the one place the
    mismatch check happens. A mismatch is an error naming the file rather than
    a silently preferred value — either half could be the corrupted one, and
    guessing which would produce an identity nobody holds.
    """
    try:
        private_key = bytes.fromhex(str(document["private_key_hex"]))
        stored_public_key = bytes.fromhex(str(document["public_key_hex"]))
    except KeyError as exc:
        raise KeyfileError(f"{path}: keyfile is missing the {exc} field") from exc
    except ValueError as exc:
        raise KeyfileError(f"{path}: keyfile holds a field that is not hex: {exc}") from exc

    if len(private_key) != PRV_KEY_SIZE:
        raise KeyfileError(
            f"{path}: private key is {len(private_key)} bytes, expected {PRV_KEY_SIZE}"
        )
    if len(stored_public_key) != PUB_KEY_SIZE:
        raise KeyfileError(
            f"{path}: public key is {len(stored_public_key)} bytes, expected {PUB_KEY_SIZE}"
        )

    identity = LocalIdentity.from_private_key(private_key)
    if identity.public_key != stored_public_key:
        raise KeyfileMismatchError(
            f"{path}: the stored public key {stored_public_key.hex()} is not the one "
            f"the stored private key derives ({identity.public_key.hex()}); refusing to "
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
    logger: Logger | None = None,
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
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(keyfile_bytes(document))

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


def keyfile_from_text(
    raw: str, source: Path | str = "<submitted>", *, logger: Logger | None = None
) -> Keyfile:
    """A keyfile from the text of one, without a filesystem.

    Milestone 8's panel imports a keyfile the operator pasted, and it has to be
    the *same* import: every check `load_keyfile` makes about what a document
    claims is made here, so a document the command line refuses is refused in
    the browser for the same reason in the same words. The one thing not checked
    here is the file's permissions, which a submitted document does not have.
    """
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyfileError(f"{source}: keyfile is not valid JSON: {exc}") from exc
    return keyfile_from_document(document, source, logger=logger)


def keyfile_from_document(
    document: object, source: Path | str = "<submitted>", *, logger: Logger | None = None
) -> Keyfile:
    """The checks a keyfile document must pass, wherever it came from."""
    log = logger or get_logger(component="keystore")
    if not isinstance(document, dict):
        raise KeyfileError(f"{source}: keyfile is not a JSON object")

    try:
        version = int(document.get("version", 0))
    except (TypeError, ValueError) as exc:
        raise KeyfileError(f"{source}: keyfile version is not a number") from exc
    if version == REMOVED_SEED_VERSION:
        # A version we wrote ourselves, so the operator is owed more than "not
        # the supported version": what the file is, and what is left to do with
        # it. There is no conversion command, and saying so stops the search.
        raise KeyfileRemovedFormatError(
            f"{source}: this is a version {REMOVED_SEED_VERSION} keyfile, which "
            "records a 32-byte seed. Seeds are no longer supported and there is no "
            "command that converts one: supply the identity's 64-byte private key "
            "with `--private-key`, or create a new identity and have peers re-add it"
        )
    if version != KEYFILE_VERSION:
        raise KeyfileError(
            f"{source}: keyfile version {version} is not the supported version "
            f"{KEYFILE_VERSION}"
        )

    identity = identity_from_document(document, source)
    try:
        node_type_value = int(document.get("node_type", int(NodeType.CHAT)))
    except (TypeError, ValueError) as exc:
        raise KeyfileError(f"{source}: keyfile node_type is not a number") from exc
    try:
        node_type: NodeType | int = NodeType(node_type_value)
    except ValueError:
        node_type = node_type_value

    warnings: list[str] = []
    burned = bool(document.get("burned", False))
    if burned:
        warnings.append(BURNED_WARNING)
        log.error("keyfile_burned", keyfile=str(source), detail=BURNED_WARNING)

    return Keyfile(
        path=Path(source),
        version=version,
        name=str(document.get("name", "")),
        node_type=node_type,
        identity=identity,
        created_at=str(document.get("created_at", "")),
        burned=burned,
        warnings=tuple(warnings),
    )


def load_keyfile(
    path: Path, *, logger: Logger | None = None
) -> Keyfile:
    """Load an identity from a keyfile, checking what the file claims."""
    log = logger or get_logger(component="keystore")
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyfileError(f"{path}: could not read the keyfile: {exc}") from exc

    parsed = keyfile_from_text(raw, path, logger=log)

    warnings = list(parsed.warnings)
    if (warning := _permission_warning(Path(path))) is not None:
        # The one check a submitted document cannot have: it is a fact about a
        # file rather than about the identity in it.
        warnings.insert(0, warning)
        log.error("keyfile_permissions_broad", keyfile=str(path), detail=warning)

    keyfile = dataclasses.replace(parsed, warnings=tuple(warnings))
    log.info("keyfile_loaded", **keyfile.as_json())
    return keyfile


ENTITY_STORE_SOURCE = "the entity store"


@dataclass(frozen=True, slots=True)
class LocalEntity:
    """One local identity in this run, whatever store it came from.

    Milestone 5 gives identities a second source, so the registry stops being a
    list of keyfiles and becomes a list of *entities that happen to know where
    they came from*. `source` is what the startup line reports, and it is the
    thing an operator needs when two runs disagree about which identity is live.
    """

    name: str
    identity: LocalIdentity
    node_type: NodeType | int
    source: str
    """`str(path)` for a keyfile, `the entity store` for a stored row."""

    keyfile: Keyfile | None = None
    warnings: tuple[str, ...] = ()

    @property
    def public_key(self) -> bytes:
        return self.identity.public_key

    @property
    def node_hash(self) -> int:
        return self.identity.node_hash

    @property
    def from_store(self) -> bool:
        return self.keyfile is None

    def as_json(self) -> dict[str, object]:
        """What an identity is, without what makes it secret."""
        return {
            "entity_name": self.name,
            "node_type": int(self.node_type),
            "public_key": self.public_key.hex(),
            "node_hash": self.node_hash,
            "source": self.source,
        }


class EntityRegistry:
    """The local entities one run holds, with the §3 rule 3 check between them.

    Two local entities sharing a node hash would make an inbound packet
    ambiguous between our *own* identities, which is the one collision case the
    design rules out rather than handles. Generation is fed the hashes already
    taken so a new key avoids them; loading two that collide is a startup failure
    naming both.

    The rule applies across **every** source in the run (design D14): a keyfile
    that collides with a stored entity is the same fault as two keyfiles that
    collide, and reporting it differently would let the mixed case through.
    """

    def __init__(self, *, logger: Logger | None = None) -> None:
        self._entities: list[LocalEntity] = []
        self._log = logger or get_logger(component="keystore")

    @property
    def entities(self) -> tuple[LocalEntity, ...]:
        return tuple(self._entities)

    @property
    def keyfiles(self) -> tuple[Keyfile, ...]:
        return tuple(e.keyfile for e in self._entities if e.keyfile is not None)

    def __len__(self) -> int:
        return len(self._entities)

    def taken_node_hashes(self) -> frozenset[int]:
        return frozenset(entity.node_hash for entity in self._entities)

    def load(self, path: Path) -> LocalEntity:
        return self._register(_from_keyfile(load_keyfile(path, logger=self._log)))

    def create(
        self,
        path: Path,
        name: str,
        *,
        node_type: NodeType | int = NodeType.CHAT,
        burned: bool = False,
    ) -> LocalEntity:
        return self._register(
            _from_keyfile(
                create_keyfile(
                    path,
                    name,
                    node_type=node_type,
                    avoid_node_hashes=self.taken_node_hashes(),
                    burned=burned,
                    logger=self._log,
                )
            )
        )

    def add_stored(
        self,
        name: str,
        identity: LocalIdentity,
        *,
        node_type: NodeType | int = NodeType.CHAT,
    ) -> LocalEntity:
        """Register an identity loaded from the entity store (design D14)."""
        return self._register(
            LocalEntity(
                name=name,
                identity=identity,
                node_type=node_type,
                source=ENTITY_STORE_SOURCE,
            )
        )

    def _register(self, entity: LocalEntity) -> LocalEntity:
        for existing in self._entities:
            if existing.node_hash == entity.node_hash:
                raise NodeHashCollisionError(
                    f"local entities {existing.name!r} (from {existing.source}) and "
                    f"{entity.name!r} (from {entity.source}) share node hash "
                    f"0x{entity.node_hash:02x}; two local entities with one node hash "
                    "make an inbound packet ambiguous between our own identities "
                    "(DESIGN.md §3)"
                )
        self._entities.append(entity)
        return entity


def _from_keyfile(keyfile: Keyfile) -> LocalEntity:
    return LocalEntity(
        name=keyfile.name,
        identity=keyfile.identity,
        node_type=keyfile.node_type,
        source=str(keyfile.path),
        keyfile=keyfile,
        warnings=keyfile.warnings,
    )
