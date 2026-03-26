from contextlib import contextmanager
from typing import Generator, Protocol

from rich.console import Console
from rich.live import Live
from rich.text import Text


class LiveLike(Protocol):
    def update(self, renderable: str | Text, refresh: bool) -> None: ...
    def stop(self) -> None: ...


class PlainLive:
    """Drop-in replacement for Live when we're in --plain mode."""

    def __init__(self, console: Console, **kwargs):
        self.console = console

    def update(self, renderable, refresh: bool | None = None):
        pass

    def stop(self):
        pass  # Live.stop() exists, so keep surface compatible if you call it


@contextmanager
def maybe_live(plain=False, **kwargs) -> Generator[LiveLike, None, None]:
    """
    If plain=False, yield a real Live(...) context.
    If plain=True, yield a PlainLive that just prints.
    """
    if not plain:
        with Live(**kwargs) as live:
            yield live
    else:
        live = PlainLive(**kwargs)
        yield live
