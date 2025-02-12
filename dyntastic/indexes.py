"""Indexes for Dyntastic"""

from typing import List, Optional

from pydantic import BaseModel


class Index(BaseModel):
    __hash_key__: str
    __range_key__: Optional[str] = None
    __index_name__: str
    __keys_only__: bool = False
    __projection_keys__: Optional[List[str]] = None

    model_config = {
        "exclude": {
            "__hash_key__",
            "__range_key__",
            "__index_name__",
            "__keys_only__",
            "__projection_keys__",
        }
    }
