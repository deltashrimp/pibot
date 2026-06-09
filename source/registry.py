from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CommandConfig:
    handler: Callable
    value: int
    dev_only: bool = False


PIBOT_COMMANDS: dict[str, CommandConfig] = {}


def pibot_command(
    name: str, value: int, dev_only: bool = False
) -> Callable[[Callable], Callable]:
    def decorator(func: Callable) -> Callable:
        PIBOT_COMMANDS[name] = CommandConfig(handler=func, value=value, dev_only=dev_only)
        return func

    return decorator
