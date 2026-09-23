class DatabaseBusy(Exception):
    """A business boundary classified a transient database contention failure."""


class DatabasePoolTimeout(Exception):
    """No connection was available within the pool acquisition budget."""


class ChatResourcesUnavailable(Exception):
    """The application has not prepared the resources needed to create ChatService."""
