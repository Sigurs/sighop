# Spec Delta

## REMOVED Requirements

### Requirement: `sighop monitor` command
**Reason**: The command line is removed. The panel's dashboard feed is the live view of mesh traffic.
**Migration**: Open the panel's overview, whose feed paints recent history and then streams live records.

### Requirement: Startup line reports what the board is
**Reason**: Removed with the command. The booted node reports the board at startup and the panel's
system page presents the modem's own reported parameters.
**Migration**: See `node-boot` and `web-dashboard`.

### Requirement: Per-frame rendering
**Reason**: Removed with the command. The node renders receptions as output and the panel's feed
renders them as records.
**Migration**: See `node-boot` and `web-dashboard`.

### Requirement: Unverified content is never rendered as verified
**Reason**: Carried forward; the rule holds on every surface that survives.
**Migration**: See `node-boot`, "Rendered output never presents unverified content as verified".

### Requirement: Periodic summary
**Reason**: Removed with the command. The node's periodic status line carries the same counters.
**Migration**: See `node-boot`, "Periodic status reports the operating limits".

### Requirement: Rendering is separable from I/O
**Reason**: Removed with the command whose testability it existed to protect.
**Migration**: None. The rendering the node still does keeps its own tests.

### Requirement: Optional simultaneous capture
**Reason**: Removed with the command.
**Migration**: None.

### Requirement: Wide events continue alongside rendered output
**Reason**: Removed with the command. The booted node emits wide events as it always has.
**Migration**: None.

### Requirement: Node discovery frames are rendered with their fields
**Reason**: Removed with the command.
**Migration**: See the panel's feed, which renders discovery records with their fields.
