"""Events for recording state changes."""
from typing import Any, Callable, Dict, Generic, Tuple, Type, TypeVar, Union, overload

from .schema import SchemaBase, SchemaMeta


# pylint: disable=invalid-name
_T_Event = TypeVar("_T_Event", bound="EventBase", contravariant=True)

_EventHandlerFunction = Callable[[Any, _T_Event], None]
_EventHandlerDecorator = Callable[
    [_EventHandlerFunction[_T_Event]], "EventHandler[_T_Event]"
]
# pylint: enable=invalid-name


class EventMeta(SchemaMeta):
    """The metaclass that all event classes must inherit from."""

    __by_name: Dict[str, "EventMeta"] = {}

    def __new__(
        cls, name: str, bases: Tuple[type, ...], attrs: Dict[str, Any], **extra
    ):
        """Construct a new Event class."""
        if name in cls.__by_name:
            raise TypeError(f"duplicate definition for event {name}")
        new_class = super().__new__(cls, name, bases, attrs, **extra)
        cls.__by_name[name] = new_class
        return new_class

    @classmethod
    def construct_named(cls, name: str, args: Dict[str, Any]) -> "EventBase":
        """Initialize and validate a newly constructed Event class."""
        return cls.__by_name[name](**args)


class EventBase(SchemaBase, metaclass=EventMeta):
    """The base class that all event classes must inherit from."""


def handle_event(evt_class: Type[_T_Event]) -> _EventHandlerDecorator[_T_Event]:
    """Decorate an aggregate method as an event handler for certain events."""

    def event_handler_decorator(handler: _EventHandlerFunction[_T_Event]):
        return EventHandler(handler, evt_class)

    return event_handler_decorator


class EventHandler(Generic[_T_Event]):
    """A descriptor for an event handler method, generated by a call to `handle_event`."""

    _evt_class: Type[_T_Event]

    def __init__(
        self, handler: _EventHandlerFunction[_T_Event], evt_class: Type[_T_Event]
    ):
        """Initialize the EventHandler."""
        self.__dict__["_handler"] = handler
        self._evt_class = evt_class

    def __set_name__(self, owner: Any, name: str):
        """Record the event handler mapping on the owner."""
        owner.__agg_events__[self._evt_class] = name

    @overload
    def __get__(self, obj: None, objtype: Any) -> "EventHandler[_T_Event]":
        """Get the EventHandler descriptor from the owner class."""
        ...

    @overload
    def __get__(self, obj: Any, objtype: Any) -> Callable[[_T_Event], None]:
        """Get the EventHandler method from the object."""
        ...

    def __get__(
        self, obj: Any, objtype=None
    ) -> Union["EventHandler[_T_Event]", Callable[[_T_Event], None]]:
        """Get the EventHandler or EventHandler descriptor from the owning object or class."""
        if obj is None:
            return self
        return lambda evt: self.__dict__["_handler"](obj, evt)
