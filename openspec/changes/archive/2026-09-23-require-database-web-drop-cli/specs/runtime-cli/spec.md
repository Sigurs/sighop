# Spec Delta

## REMOVED Requirements

### Requirement: The run command wires the platform together
**Reason**: The command line is removed; the node is booted by an argument-free entry point.
**Migration**: See `node-boot`, "The node is started by an entry point that takes no arguments".

### Requirement: Transmission requires an explicit flag
**Reason**: There is no flag to give; the transmit gate is keyed from the environment.
**Migration**: See `node-boot`, "Transmission stays off unless the environment enables it".

### Requirement: Periodic status reports the operating limits
**Reason**: Carried forward unchanged to the capability that owns the booted node.
**Migration**: See `node-boot`, "Periodic status reports the operating limits".

### Requirement: Rendered output never presents unverified content as verified
**Reason**: Carried forward unchanged to the capability that owns the booted node.
**Migration**: See `node-boot`, "Rendered output never presents unverified content as verified".

### Requirement: Entity identities come from the entity store or from keyfiles
**Reason**: Keyfiles were supplied on the command line and there is no command line. A database is
now required, so the entity store is the only source an identity can come from.
**Migration**: See `node-boot`, "Local identities come from the entity store". An identity held only
as a keyfile must be imported through the panel before this change is applied.

### Requirement: The database is configured on the command line or from the environment
**Reason**: There is no command line to override the environment, and no database is no longer a
mode the node can start in.
**Migration**: See `node-boot`, "Starting without a database is a startup failure".

### Requirement: Startup reports what persistence restored
**Reason**: Carried forward to the capability that owns the booted node, without its in-memory case.
**Migration**: See `node-boot`, "Startup reports what persistence restored".

### Requirement: A database command applies and reports migrations
**Reason**: The command surface is removed. Applying migrations is no longer optional, so there is
nothing left to opt into.
**Migration**: See `node-boot`, "Outstanding migrations are applied before the node serves".

### Requirement: A replay run does not write to the database by default
**Reason**: The node no longer replays captures; replay exists only as the build's parity harness.
**Migration**: See `capture-replay`, "A capture is replayed by a module entry point".

### Requirement: Identity management commands cover the entity store
**Reason**: The command surface is removed; the panel's identity pages are the surface.
**Migration**: Use the panel's identities pages, which make the same repository calls.

### Requirement: A key management command creates and inspects identities
**Reason**: The command surface is removed; the panel's identity pages are the surface.
**Migration**: Use the panel's identity create, import, export and detail pages.

### Requirement: A key management command removes a stored identity
**Reason**: The command surface is removed; the panel's identity pages are the surface.
**Migration**: Use the panel's identity removal confirmation.

### Requirement: A message can be sent to a selected peer
**Reason**: The command surface is removed; the panel's chat pages are the surface.
**Migration**: Use the panel's conversation pages.

### Requirement: Direct message activity is rendered as run output
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "Direct message, room, bot and channel activity is rendered as output".

### Requirement: A room command surface manages rooms, passwords and members
**Reason**: The command surface is removed; the panel's room pages are the surface.
**Migration**: Use the panel's room administration pages.

### Requirement: A password is never accepted as a command-line argument
**Reason**: There is no command line, so there is no argument through which a password could be
published to every user on the host.
**Migration**: None. Passwords are entered in the panel's forms.

### Requirement: Rotating a password and revoking a member each state their consequence
**Reason**: The command surface is removed; the panel states the same consequence.
**Migration**: Use the panel's password rotation and member revocation confirmations.

### Requirement: A run serves the rooms that are configured and says what it is serving
**Reason**: Carried forward to the capability that owns the booted node, without its no-database case.
**Migration**: See `node-boot`, "The node serves the rooms, bots, channels and webhooks that are configured and says so".

### Requirement: A bot command surface manages bots, their mode and their configuration
**Reason**: The command surface is removed; the panel's bot pages are the surface.
**Migration**: Use the panel's bot administration pages.

### Requirement: A run runs the bots that are configured and says what it is running
**Reason**: Carried forward to the capability that owns the booted node, without its no-database case.
**Migration**: See `node-boot`, "The node serves the rooms, bots, channels and webhooks that are configured and says so".

### Requirement: Bot activity is rendered as run output
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "Direct message, room, bot and channel activity is rendered as output".

### Requirement: A run serves the web interface when asked and says where
**Reason**: The interface is no longer optional and there are no options to ask with.
**Migration**: See `web-server`, "The web interface is always served and its address comes from the environment".

### Requirement: Direct message activity from the interface is rendered as run output
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "Direct message, room, bot and channel activity is rendered as output".

### Requirement: A web account command surface manages the accounts that sign in to the interface
**Reason**: The command surface is removed and this change adds no panel replacement. Account
management is deliberately lost for now.
**Migration**: None. The account created by first-run setup is the only account; see `web-auth`,
"Operator accounts are durable, hashed, and created only by first-run setup".

### Requirement: A webhook command surface manages webhooks
**Reason**: The command surface is removed; the panel's webhook pages are the surface.
**Migration**: Use the panel's webhook administration pages.

### Requirement: A run reports the webhooks it will deliver to
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "The node serves the rooms, bots, channels and webhooks that are configured and says so".

### Requirement: A channel command surface manages channels
**Reason**: The command surface is removed; the panel's chat page administers channels.
**Migration**: Use the panel's channel administration controls on the chat page. Reading back a
stored pre-shared key, which only this surface did, has no replacement and is deliberately lost; see
`channel-store`, "A stored pre-shared key is never read back".

### Requirement: A run reports the channels it has loaded
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "The node serves the rooms, bots, channels and webhooks that are configured and says so".

### Requirement: Channel activity is rendered as run output
**Reason**: Carried forward to the capability that owns the booted node.
**Migration**: See `node-boot`, "Direct message, room, bot and channel activity is rendered as output".

### Requirement: A room command deletes a room
**Reason**: The command surface is removed; the panel deletes rooms behind a guarded confirmation.
**Migration**: Use the panel's room deletion confirmation.

### Requirement: A bot command deletes a bot
**Reason**: The command surface is removed; the panel deletes bots behind a guarded confirmation.
**Migration**: Use the panel's bot deletion confirmation.

### Requirement: The keys, room, channel and webhook surfaces each rename what they manage
**Reason**: The command surfaces are removed; the panel renames each of them.
**Migration**: Use the panel's rename forms for identities, rooms, channels and webhooks.

### Requirement: A run reports the path hash size in force
**Reason**: Carried forward to the capability that owns the booted node, which reports every setting
in force at startup.
**Migration**: See `node-boot`, "Every setting is read from a named environment variable", and
`path-hash-size`.
