"""The S3 backend under the name it had before v3.1.2.

Kept so anything that imports `MinIOBackend` keeps working. New code imports
`S3Backend` from `.s3_backend`.
"""
from .s3_backend import S3Backend as MinIOBackend

__all__ = ["MinIOBackend"]
