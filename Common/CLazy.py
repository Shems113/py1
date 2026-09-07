class CLazy:
    def __init__(self, factory):
        self._factory = factory
        self._value = None
        self._is_created = False

    @property
    def value(self):
        if not self._is_created:
            self._value = self._factory()
            self._is_created = True
        return self._value

    @property
    def is_value_created(self):
        return self._is_created
