"""In-process event bus.

Event op/data payloads are the [8-C] Server -> Client wire schema verbatim; the
WebSocket hub forwards what is published here without reshaping it. Every event
carries a monotonically increasing ``seq`` ([8-C] reliability).

Ops emitted by Graph Core ([8-C]):
    node.add        data = Node
    node.update     data = { id, patch }          # patch 규약 = [5-A]
    node.delete     data = { id }
    edge.add        data = Edge
    edge.update     data = { source, target, relation, key, patch }
    edge.delete     data = { source, target, relation, key }
    finding.add     data = Finding                # [5-G], [23-B]
    finding.update  data = { finding_id, patch }
    finding.delete  data = { finding_id }

Coalescing into graph.batch and the seq-based resync procedure belong to the hub
([8-C], TASK 6).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

NODE_ADD = "node.add"
NODE_UPDATE = "node.update"
NODE_DELETE = "node.delete"
EDGE_ADD = "edge.add"
EDGE_UPDATE = "edge.update"
EDGE_DELETE = "edge.delete"
FINDING_ADD = "finding.add"
FINDING_UPDATE = "finding.update"
FINDING_DELETE = "finding.delete"
CLEAR = "clear"


@dataclass(frozen=True)
class Event:
    """A single Server -> Client event ([8-C])."""

    op: str
    data: dict[str, Any]
    seq: int


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._seq = 0

    @property
    def seq(self) -> int:
        """Seq of the most recently published event (0 = nothing published yet)."""
        return self._seq

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """Register handler; returns a callable that unsubscribes it."""
        self._handlers.append(handler)

        def unsubscribe() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, op: str, data: dict[str, Any]) -> Event:
        """[8-C] Fan an event out to every subscriber, isolating each one.

        [13-B] CH1(2): a raising handler used to take the rest of the fan-out
        with it *and* propagate into the mutation that published the event — by
        which point the graph had already changed and the seq was already spent.
        The caller was told the operation failed while it had in fact succeeded,
        and the event never reached the wire (M1 has no resync trigger, so the
        client never learns it missed one). ``_send_all`` already isolates per
        connection for the same reason; the layer above it did not.

        Nothing after a committed mutation may turn that mutation into a
        reported failure.
        """
        self._seq += 1
        event = Event(op=op, data=data, seq=self._seq)
        for handler in list(self._handlers):
            try:
                handler(event)
            except Exception:  # noqa: BLE001
                log.exception("event subscriber failed for %s (seq=%s)", op, event.seq)
        return event
