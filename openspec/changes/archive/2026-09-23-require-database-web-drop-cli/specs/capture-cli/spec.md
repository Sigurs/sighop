# Spec Delta

## REMOVED Requirements

### Requirement: `sighop capture` command
**Reason**: The command line is removed. Nothing records new captures; the committed corpus stays
readable and the runtime keeps its own capture writing for a future change to expose.
**Migration**: None. Record captures with a build from before this change if new evidence is needed.

### Requirement: Capture record format
**Reason**: Removed with the command that wrote it. The format is still read.
**Migration**: See `capture-replay`, which continues to define how a capture file is read.

### Requirement: Append-only, crash-safe writes
**Reason**: Removed with the command that wrote captures.
**Migration**: None.

### Requirement: Graceful shutdown
**Reason**: Removed with the command that wrote captures.
**Migration**: None.

### Requirement: Periodic heartbeat logging
**Reason**: Removed with the command that wrote captures.
**Migration**: None.

### Requirement: Capture provenance header record
**Reason**: Removed with the command that wrote it. Replay still surfaces provenance from captures
already recorded.
**Migration**: See `capture-replay`.

### Requirement: Provenance values are observed, never inferred
**Reason**: Removed with the command that recorded them.
**Migration**: None.

### Requirement: Appending to an existing capture file
**Reason**: Removed with the command that wrote captures.
**Migration**: None.
