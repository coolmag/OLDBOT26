import asyncio
import logging
from typing import Callable, Dict, List, Any

logger = logging.getLogger(__name__)

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: str, callback: Callable):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
        logger.debug(f"Subscribed {callback.__name__} to {event_type}")

    async def publish(self, event_type: str, data: Any = None):
        if event_type in self._subscribers:
            tasks = [callback(data) for callback in self._subscribers[event_type]]
            await asyncio.gather(*tasks)
            logger.debug(f"Published {event_type} with data {data}")
