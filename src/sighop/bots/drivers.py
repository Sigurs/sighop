"""The driver registry: a name, a class, and nothing that imports by string.

Design D14. `sighop bot create --driver greeter` looks a name up in this dict
and refuses an unknown one by listing what exists. There is no entry-point
discovery and no import by string: loading foreign code into the process that
holds the entity seeds needs a better reason than convenience, and out-of-process
plugins are an explicit Non-Goal of this milestone rather than an oversight.

Configuration validation is routed here too, because two of the keys belong to
the runtime rather than to any driver: `rate_per_hour` and `burst` bound how much
airtime one automated decision path may claim, and a driver has no business
answering for them.
"""

from __future__ import annotations

from sighop.bots.base import (
    DEFAULT_BURST,
    DEFAULT_RATE_PER_HOUR,
    RESERVED_CONFIG_KEYS,
    Bot,
    BotConfigError,
    UnknownDriverError,
)
from sighop.bots.greeter import GreeterBot

DRIVERS: dict[str, type[Bot]] = {GreeterBot.driver_name: GreeterBot}
"""Every driver this build can run. In-tree classes, reviewed with the rest of
the code, exactly as design D14 asks."""


def driver_names() -> tuple[str, ...]:
    return tuple(sorted(DRIVERS))


def lookup(name: str) -> type[Bot]:
    """A driver class by name, or a refusal that lists what exists."""
    driver = DRIVERS.get(name)
    if driver is None:
        raise UnknownDriverError(
            f"no driver named {name!r}; this build has {', '.join(driver_names())}"
        )
    return driver


def build(name: str, config: dict) -> Bot:
    """One driver instance, configured. Raises for an unknown name."""
    return lookup(name)(config=dict(config))  # type: ignore[call-arg]


def default_config(name: str) -> dict[str, object]:
    """What a newly created bot of this driver starts with, limits included."""
    driver = lookup(name)
    return {
        **driver.default_config(),
        "rate_per_hour": DEFAULT_RATE_PER_HOUR,
        "burst": DEFAULT_BURST,
    }


def validate_config(name: str, key: str, value: str) -> object:
    """Parse one configured value: the runtime's two keys here, the rest by
    the driver that owns them."""
    if key in RESERVED_CONFIG_KEYS:
        return _validate_limit(key, value)
    return lookup(name).validate_config(key, value)


def _validate_limit(key: str, value: str) -> object:
    """The two keys the runtime owns, refused with what they bound and why."""
    if key == "burst":
        try:
            burst = int(value)
        except ValueError as exc:
            raise BotConfigError(f"burst is a whole number of actions; {value!r} is not") from exc
        if burst < 1:
            raise BotConfigError(
                "a burst below 1 would refuse every action, which is what "
                "disabling the bot is for"
            )
        return burst
    try:
        rate = float(value)
    except ValueError as exc:
        raise BotConfigError(
            f"rate_per_hour is a sustained rate in actions per hour; {value!r} is not"
        ) from exc
    if rate < 0:
        raise BotConfigError("rate_per_hour cannot be negative")
    return rate
