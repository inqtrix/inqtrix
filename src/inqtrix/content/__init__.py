"""Content layer: user-uploaded files (registry + blob access rules).

Metadata and authorization facts live in the file registry (memory or
Postgres); the bytes live in the object store
(:mod:`inqtrix.storage.object_store`). The
:class:`~inqtrix.services.file_service.FileService` is the only
consumer that combines both — routers never touch a store directly.
"""

from inqtrix.content.memory import MemoryFileRegistry
from inqtrix.content.ports import FileNotFound, FileRecord, FileRegistry

__all__ = [
    "FileNotFound",
    "FileRecord",
    "FileRegistry",
    "MemoryFileRegistry",
]
