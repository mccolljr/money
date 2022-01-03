from __future__ import annotations
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Generic, List, Tuple, Type, TypeVar

from money.framework import schema
from money.framework.event import EventBase, EventMeta

if TYPE_CHECKING:
    from money.framework.storage import Storage

EventRoot = TypeVar("EventRoot", bound=EventBase)


class AggregateDefinitionError(Exception):
    """An exception caused by an invalid aggregate class definition."""

    def __init__(self, agg_name: str, problem: str):
        self.agg_name = agg_name
        self.problem = problem
        super().__init__(f"{self.agg_name}: {self.problem}")


class AggregateMeta(schema.SchemaMeta, ABCMeta):
    """The metaclass that all aggregate classes must inherit from."""

    __agg_id__: str
    __agg_name__: str
    __agg_mixin__: bool
    __agg_events__: Dict[EventMeta, str]
    __agg_create__: EventMeta

    def __new__(cls, name: str, bases: Tuple[type, ...], attrs: Dict[str, Any]):
        # create aggregate metadata
        if not attrs.setdefault("__agg_mixin__", False):
            attrs.setdefault("__agg_id__", "id")
            attrs.setdefault("__agg_name__", name)
            attrs.setdefault("__agg_events__", {})
        new_class = super().__new__(cls, name, bases, attrs)
        return new_class

    def __init__(cls, name: str, bases: Tuple[type, ...], attrs: Dict[str, Any]):
        super().__init__(name, bases, attrs)
        if not cls.__agg_mixin__:
            AggregateMeta._validate_aggregate_id(cls)
            AggregateMeta._validate_creation_event(cls)
            AggregateMeta._validate_load_events_method(cls)

    @staticmethod
    def _validate_aggregate_id(the_cls: AggregateMeta):
        if not (the_cls.__agg_id__ and the_cls.__agg_id__ in the_cls.__schema__):
            raise AggregateDefinitionError(
                the_cls.__name__, "must define an id field, or must specify __agg_id__"
            )

    @staticmethod
    def _validate_creation_event(the_cls: AggregateMeta):
        if not hasattr(the_cls, "__agg_create__"):
            raise AggregateDefinitionError(
                the_cls.__name__, "must specify a creation event in __agg_create__"
            )
        if the_cls.__agg_create__ not in the_cls.__agg_events__:
            raise AggregateDefinitionError(
                the_cls.__name__,
                f"must specify a handler for creation event {the_cls.__agg_create__.__name__}",
            )

    @staticmethod
    def _validate_load_events_method(the_cls: AggregateMeta):
        load_events = getattr(the_cls, "load_events", None)
        if load_events is None or getattr(load_events, "__isabstractmethod__", False):
            raise AggregateDefinitionError(
                the_cls.__name__, "must define a load_events class method"
            )
        if not callable(load_events):
            raise AggregateDefinitionError(
                the_cls.__name__, "the load_events method must be callable"
            )


TAggSelf = TypeVar("TAggSelf", bound="AggregateBase")


class AggregateBase(Generic[EventRoot], schema.SchemaBase, metaclass=AggregateMeta):
    """The base class that all aggregate classes must inherit from."""

    __agg_mixin__ = True

    def apply_event(self, event: EventRoot):
        handler_name = type(self).__agg_events__[type(event)]
        getattr(self, handler_name)(event)

    @classmethod
    @abstractmethod
    async def load_events(
        cls, storage: Storage, ids: List[Any]
    ) -> Dict[Any, List[EventRoot]]:
        ...

    @classmethod
    async def load(cls: Type[TAggSelf], storage: Storage, agg_id: Any) -> TAggSelf:
        events = (await cls.load_events(storage, [agg_id])).get(agg_id, [])
        if not events:
            raise ValueError(f"no {cls.__name__} with id {agg_id}")
        if not isinstance(events[0], cls.__agg_create__):
            raise ValueError("first event must be creation event")
        loaded = cls.__new__(cls)
        for loaded_evt in events:
            loaded.apply_event(loaded_evt)
        return loaded

    @classmethod
    async def load_all(
        cls: Type[TAggSelf], storage: Storage, ids: List[Any]
    ) -> List[TAggSelf]:
        events = await cls.load_events(storage, ids)
        return [cls.from_events(evts) for evts in events.values()]

    @classmethod
    def from_events(cls: Type[TAggSelf], events: List[EventRoot]) -> TAggSelf:
        if not events:
            raise ValueError(f"no {cls.__name__} with id {id}")
        if not isinstance(events[0], cls.__agg_create__):
            raise ValueError("first event must be creation event")
        loaded = cls.__new__(cls)
        for src_evt in events:
            loaded.apply_event(src_evt)
        return loaded
