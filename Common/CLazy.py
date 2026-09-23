from collections.abc import Callable
from typing import Generic, TypeVar, cast

T = TypeVar('T')

class CLazy(Generic[T]):
    def __init__(self, factory: Callable[[], T]):
        self._factory = factory
        self._value: T | None = None
        self._is_created = False

    @property
    def value(self) -> T:
        if not self._is_created:
            self._value = self._factory()
            self._is_created = True
        return cast(T, self._value)

    @property
    def is_value_created(self) -> bool:
        return self._is_created
