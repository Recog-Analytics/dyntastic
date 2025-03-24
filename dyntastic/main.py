import os
import time
import warnings
from decimal import Decimal
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Generic,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import boto3

from .constants import dynamodb_to_cosmosdb_operator_mapping
from .indexes import Index as _Index

try:
    # Python 3.8+
    import importlib.metadata as _metadata
except ModuleNotFoundError:  # pragma: no cover
    # Python 3.7
    import importlib_metadata as _metadata  # type: ignore[no-redef, unused-ignore]

from collections import defaultdict
from contextvars import ContextVar

from azure.cosmos import CosmosClient
from azure.cosmos.container import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError
from boto3.dynamodb.conditions import ConditionBase
from pydantic import BaseModel, PrivateAttr

from . import attr, pydantic_compat, transact
from .attr import Attr, _UpdateAction, translate_updates
from .batch import BatchWriter, invoke_with_backoff
from .exceptions import DoesNotExist
from .transact import current_transaction_writer

__version__ = _metadata.version("dyntastic")

_T = TypeVar("_T", bound="Dyntastic")


class HostProvider(Enum):
    AWS = "aws"
    AZURE = "azure"


host_provider = HostProvider(os.getenv("HOST_PROVIDER", HostProvider.AWS.value))


class _TableMetadata:
    __table_name__: Union[str, Callable[[], str]]
    __table_region__: Optional[str] = None
    __table_host__: Optional[str] = None

    __hash_key__: str
    __range_key__: Optional[str] = None
    __indexes__: List[_Index] = []


class ResultPage(Generic[_T]):
    def __init__(self, items: List[_T], last_evaluated_key: Optional[dict]):
        self.items = items
        self.last_evaluated_key = last_evaluated_key
        self.has_more = last_evaluated_key is not None

    def __str__(self):
        return f"ResultPage: {self.__dict__}"

    def __repr__(self):
        return str(self)


class Index:
    def __init__(
        self,
        hash_key: str,
        range_key: Optional[str] = None,
        index_name: Optional[str] = None,
        keys_only: bool = False,
    ):
        self.hash_key = hash_key
        self.range_key = range_key
        # TODO: support INCLUDE projection?
        self.projection = "KEYS_ONLY" if keys_only else "ALL"

        if not index_name:
            if range_key:
                index_name = f"{hash_key}_{range_key}-index"
            else:
                index_name = f"{hash_key}-index"

        self.index_name = index_name


class Dyntastic(_TableMetadata, pydantic_compat.BaseModel):
    _dyntastic_unrefreshed: bool = PrivateAttr(default=False)
    _dyntastic_missing_attributes_from_index: bool = PrivateAttr(default=False)
    _dyntastic_batch_writer: ContextVar[Optional[BatchWriter]]

    @classmethod
    def _get_cosmos_client(cls) -> ContainerProxy:
        secret = os.getenv("COSMOS_SECRET")
        account_name = os.getenv("COSMOS_ACCOUNT_NAME")
        database_name = os.getenv("COSMOS_DATABASE_NAME")
        if not secret or not account_name or not database_name:
            raise ValueError(
                "COSMOS_SECRET, COSMOS_ACCOUNT_NAME, and COSMOS_DATABASE_NAME environment variables must be set"
            )
        uri = f"https://{account_name}.documents.azure.com:443/"
        cosmos_client = CosmosClient(
            uri,
            credential=secret,
        )
        database_client = cosmos_client.get_database_client(database_name)
        return database_client.get_container_client(cls.__table_name__)

    @classmethod
    def get_model(cls, item: dict):
        """Get a model instance from a DynamoDB item.

        This method can be overridden to support a single-table design pattern
        (i.e. multiple schemas shared in a single table).
        """

        return cls

    @classmethod
    def _dyntastic_load_model(cls, item: dict, load_full_item: bool = False):
        model = cls.get_model(item)

        data, had_validation_errors = pydantic_compat.try_model_construct(model, item)
        if had_validation_errors:
            # assume KEYS_ONLY or INCLUDE index
            data._dyntastic_missing_attributes_from_index = True

        if load_full_item:
            data.refresh()

        return data

    @classmethod
    def _serialize_key(
        cls,
        method: str,
        hash_key: Any,
        range_key: Any,
        hash_key_type: Optional[Type] = None,
        range_key_type: Optional[Type] = None,
    ) -> dict:
        key = {cls.__hash_key__: hash_key}
        if cls.__range_key__:
            key[cls.__range_key__] = range_key

        if hash_key_type is None:
            hash_key_type = pydantic_compat.field_type(cls, cls.__hash_key__)

        # hash key checks

        if not isinstance(hash_key, hash_key_type):
            raise ValueError(
                f"Expected hash key to be of type {hash_key_type.__name__}, "
                f"got {type(hash_key).__name__} in {cls.__name__}.{method}()"
            )

        if cls.__range_key__ is None:
            if range_key is not None:
                raise ValueError(
                    f"Range key `{range_key}` provided to {cls.__name__}.{method}(), "
                    "but table does not have a range key"
                )
            return attr.serialize(key)

        # range key checks

        if range_key_type is None:
            range_key_type = pydantic_compat.field_type(cls, cls.__range_key__)

        if range_key is None:
            raise ValueError(
                f"Range key required but not provided to {cls.__name__}.{method}()"
            )

        # TODO: In order to run the following check, we would need to support
        #       *deserializing* the range key e.g. from a string to a datetime, just
        #       for this check, then re-serialize it before sending to DynamoDB

        # if not isinstance(range_key, range_key_type):
        #     raise ValueError(
        #         f"Expected range key to be of type {range_key_type.__name__}, "
        #         f"got {type(range_key).__name__} in {cls.__name__}.{method}()"
        #     )

        return attr.serialize(key)

    @classmethod
    def get_aws(
        cls: Type[_T], hash_key, range_key=None, *, consistent_read: bool = False
    ) -> _T:
        serialized_key = cls._serialize_key("get", hash_key, range_key)
        response = cls._dynamodb_table().get_item(
            Key=serialized_key, ConsistentRead=consistent_read
        )
        data = response.get("Item")
        if data:
            return cls._dyntastic_load_model(data)
        else:
            raise DoesNotExist

    @classmethod
    def safe_get_aws(
        cls: Type[_T], hash_key, range_key=None, *, consistent_read: bool = False
    ) -> Optional[_T]:
        try:
            return cls.get_aws(
                hash_key, range_key=range_key, consistent_read=consistent_read
            )
        except DoesNotExist:
            return None

    @classmethod
    def get_azure(
        cls: Type[_T], hash_key, range_key=None, *, consistent_read: bool = False
    ) -> _T:
        container_client = cls._get_cosmos_client()

        document_id = str(hash_key)

        try:
            if cls.__range_key__ and not range_key:
                raise ValueError(
                    f"Range key required but not provided to {cls.__name__}.get_azure()"
                )

            # partition key is the range key since our implementation of range keys is
            # to just use the hash key as id and the range key as the partition key
            read_item_args = {}
            if cls.__range_key__ and range_key:
                read_item_args["partition_key"] = range_key
            else:
                read_item_args["partition_key"] = document_id
            item = container_client.read_item(item=document_id, **read_item_args)
            return cls._cosmos_to_model(item)
        except CosmosHttpResponseError as e:
            if e.status_code == 404:
                raise DoesNotExist(f"Item with key {document_id} does not exist")
            raise

    @classmethod
    def safe_get_azure(
        cls: Type[_T], hash_key, range_key=None, *, consistent_read: bool = False
    ) -> Optional[_T]:
        try:
            return cls.get_azure(
                hash_key, range_key=range_key, consistent_read=consistent_read
            )
        except DoesNotExist:
            return None

    @classmethod
    def safe_get(
        cls: Type[_T], hash_key, range_key=None, *, consistent_read: bool = False
    ) -> Optional[_T]:
        if host_provider == HostProvider.AWS:
            return cls.safe_get_aws(
                hash_key, range_key, consistent_read=consistent_read
            )
        elif host_provider == HostProvider.AZURE:
            return cls.safe_get_azure(
                hash_key, range_key, consistent_read=consistent_read
            )
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    @classmethod
    def batch_get(
        cls: Type[_T],
        keys: Union[List[Any], List[Tuple[Any, Any]]],
        consistent_read: bool = False,
    ) -> List[_T]:
        hash_key_type = pydantic_compat.field_type(cls, cls.__hash_key__)
        range_key_type = None
        if cls.__range_key__:
            range_key_type = pydantic_compat.field_type(cls, cls.__range_key__)

        serialized_keys = []
        for key in keys:
            if cls.__range_key__ and (
                not isinstance(key, (list, tuple)) or len(key) != 2
            ):
                raise ValueError(
                    f"Must provide (hash_key, range_key) tuples as `keys` to {cls.__name__}.batch_get(), got {key}"
                )
            hash_key, range_key = key if cls.__range_key__ else (key, None)
            serialized_key = cls._serialize_key(
                "batch_get", hash_key, range_key, hash_key_type, range_key_type
            )
            serialized_keys.append(serialized_key)

        responses = invoke_with_backoff(
            cls._dynamodb_resource().batch_get_item,
            {
                cls._resolve_table_name(): {
                    "Keys": serialized_keys,
                    "ConsistentRead": consistent_read,
                }
            },
            "UnprocessedKeys",
        )

        items: List[_T] = []
        for response in responses:
            raw_items = response["Responses"][cls._resolve_table_name()]
            items.extend(cls._dyntastic_load_model(item) for item in raw_items)

        return items

    @classmethod
    def query(
        cls: Type[_T],
        hash_key,
        *,
        consistent_read: bool = False,
        range_key_condition=None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> Generator[_T, None, None]:
        if host_provider == HostProvider.AWS:
            return cls.query_aws(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )
        elif host_provider == HostProvider.AZURE:
            return cls.query_azure(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    @classmethod
    def query_azure(
        cls: Type[_T],
        hash_key,
        *,
        consistent_read: bool = False,
        range_key_condition=None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> Generator[_T, None, None]:
        while True:
            result = cls.query_page_azure(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )

            last_evaluated_key = result.last_evaluated_key
            yield from result.items

            if not result.has_more:
                break  # pragma: no cover (in python 3.8/3.9, this appeared as missing coverage)

        # Add filter condition if provided

    @classmethod
    def query_page_azure(
        cls: Type[_T],
        hash_key: Union[str, ConditionBase],
        *,
        consistent_read: bool = False,
        range_key_condition: Optional[ConditionBase] = None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> ResultPage[_T]:
        container_client = cls._get_cosmos_client()

        # Build the query
        query = "SELECT * FROM c"
        if hash_key:
            query += f" WHERE {cls._condition_to_cosmos(cls, hash_key)}"
        if range_key_condition:
            query += f" AND {cls._condition_to_cosmos(cls, range_key_condition)}"
        if filter_condition:
            query += f" AND {cls._condition_to_cosmos(cls, filter_condition)}"
        if scan_index_forward:
            if range_key_condition and index:
                query += f" ORDER BY c.{index.__range_key__} ASC"
            else:
                query += f" ORDER BY c.{cls.__range_key__} ASC"
        else:
            if range_key_condition and index:
                query += f" ORDER BY c.{index.__range_key__} DESC"
            else:
                query += f" ORDER BY c.{cls.__range_key__} DESC"
        results = container_client.query_items(
            query=query,
            enable_cross_partition_query=True,
        )

        items = [cls._cosmos_to_model(item) for item in results]
        return ResultPage(items, None)

    def _condition_to_cosmos(self, condition: Union[str, ConditionBase]) -> str:
        if isinstance(condition, ConditionBase):
            expression = condition.get_expression()
            expression_operator = expression["operator"]
            expression_values = expression["values"]
            field_name = expression_values[0].name
            comparison_value = expression_values[1]
            if isinstance(comparison_value, str):
                comparison_value = f"'{comparison_value}'"
            elif isinstance(comparison_value, (int, float)):
                comparison_value = str(comparison_value)

            sql_operator = dynamodb_to_cosmosdb_operator_mapping.get(
                expression_operator, expression_operator
            )

            if sql_operator == "BETWEEN":
                return f"c.{field_name} BETWEEN {comparison_value[0]} AND {comparison_value[1]}"
            elif sql_operator == "IN":
                values_str = ", ".join(
                    [
                        f"'{v}'" if isinstance(v, str) else str(v)
                        for v in comparison_value
                    ]
                )
                return f"c.{field_name} IN ({values_str})"
            elif sql_operator == "LIKE":
                return f"c.{field_name} LIKE '{comparison_value}%'"
            else:
                return f"c.{field_name} {sql_operator} {comparison_value}"
        else:
            return f"c.{self.__hash_key__} = '{condition}'"

    @classmethod
    def query_aws(
        cls: Type[_T],
        hash_key,
        *,
        consistent_read: bool = False,
        range_key_condition=None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> Generator[_T, None, None]:
        while True:
            result = cls.query_page_aws(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )

            last_evaluated_key = result.last_evaluated_key
            yield from result.items

            if not result.has_more:
                break  # pragma: no cover (in python 3.8/3.9, this appeared as missing coverage)

    @classmethod
    def query_page(
        cls: Type[_T],
        hash_key: Union[str, ConditionBase],
        *,
        consistent_read: bool = False,
        range_key_condition: Optional[ConditionBase] = None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> ResultPage[_T]:
        if host_provider == HostProvider.AWS:
            return cls.query_page_aws(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )
        elif host_provider == HostProvider.AZURE:
            return cls.query_page_azure(
                hash_key,
                consistent_read=consistent_read,
                range_key_condition=range_key_condition,
                filter_condition=filter_condition,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                scan_index_forward=scan_index_forward,
                load_full_item=load_full_item,
            )
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    @classmethod
    def query_page_aws(
        cls: Type[_T],
        hash_key: Union[str, ConditionBase],
        *,
        consistent_read: bool = False,
        range_key_condition: Optional[ConditionBase] = None,
        filter_condition: Optional[ConditionBase] = None,
        index: Optional[_Index] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        scan_index_forward: bool = True,
        load_full_item: bool = False,
    ) -> ResultPage[_T]:
        if index and consistent_read:
            raise ValueError(
                "Cannot perform a consistent read against a secondary index"
            )

        if isinstance(hash_key, ConditionBase):
            key_condition = hash_key
        elif index is not None:
            raise ValueError(
                "Must specify attribute condition for index, e.g. A.my_index_hash_key == 'example_value'"
            )
        else:
            key_condition: ConditionBase = Attr(cls.__hash_key__) == hash_key  # type: ignore

        if range_key_condition:
            key_condition &= range_key_condition

        if index:
            index_name = index.__index_name__
        else:
            index_name = None

        response = cls._dyntastic_call(
            "query",
            ConsistentRead=consistent_read,
            IndexName=index_name,
            Limit=per_page,
            ExclusiveStartKey=last_evaluated_key,
            KeyConditionExpression=key_condition,
            FilterExpression=filter_condition,
            ScanIndexForward=scan_index_forward,
        )

        raw_items = response.get("Items")
        items = [
            cls._dyntastic_load_model(item, load_full_item=load_full_item)
            for item in raw_items
        ]
        last_evaluated_key = response.get("LastEvaluatedKey")

        return ResultPage(items, last_evaluated_key)

    @classmethod
    def scan(
        cls: Type[_T],
        filter_condition: Optional[ConditionBase] = None,
        *,
        consistent_read: bool = False,
        index: Optional[str] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        load_full_item: bool = False,
    ):
        if host_provider == HostProvider.AWS:
            return cls.scan_aws(
                filter_condition=filter_condition,
                consistent_read=consistent_read,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                load_full_item=load_full_item,
            )
        elif host_provider == HostProvider.AZURE:
            return cls.scan_azure(
                filter_condition=filter_condition,
                consistent_read=consistent_read,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                load_full_item=load_full_item,
            )
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    @classmethod
    def scan_azure(
        cls: Type[_T],
        filter_condition: Optional[ConditionBase] = None,
        *,
        consistent_read: bool = False,
        index: Optional[str] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        load_full_item: bool = False,
    ):
        container_client: ContainerProxy = cls._get_cosmos_client()

        # Build the query
        query = "SELECT * FROM c"
        parameters = []

        # Add filter condition if provided
        if filter_condition:
            # Convert DynamoDB filter condition to Cosmos DB SQL WHERE clause
            where_clause = cls._convert_filter_to_cosmos(filter_condition)
            if where_clause:
                query += f" WHERE {where_clause}"

        # Handle pagination
        if per_page:
            query += (
                f" OFFSET {last_evaluated_key.get('offset', 0)} LIMIT {per_page}"
                if last_evaluated_key
                else f" LIMIT {per_page}"
            )

        # Execute query
        query_iterable = container_client.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True,  # Enable cross-partition query
            max_item_count=per_page,
        )

        # Process results
        items = []
        continuation_token = None

        for item in query_iterable:
            items.append(cls._cosmos_to_model(item, load_full_item))

            # Get continuation token if available
            if hasattr(query_iterable, "continuation_token"):
                continuation_token = query_iterable.continuation_token
        # Prepare last evaluated key
        if continuation_token:
            last_evaluated_key = {
                "continuation_token": continuation_token,
                "offset": (last_evaluated_key.get("offset", 0) + len(items))
                if last_evaluated_key
                else len(items),
            }
        else:
            last_evaluated_key = None

        yield from items

    @classmethod
    def _convert_filter_to_cosmos(cls, filter_condition: ConditionBase) -> str:
        """
        Convert DynamoDB filter condition to Cosmos DB SQL WHERE clause
        """
        if not filter_condition:
            return ""

        # Implementation depends on your ConditionBase structure
        # Example conversion:
        operator_map = {
            "=": "=",
            "<>": "!=",
            "<": "<",
            "<=": "<=",
            ">": ">",
            ">=": ">=",
            "BETWEEN": "BETWEEN",
            "IN": "IN",
            "contains": "CONTAINS",
            "begins_with": "STARTSWITH",
        }

        # Basic conversion - extend based on your needs
        if (
            hasattr(filter_condition, "operator")
            and hasattr(filter_condition, "attribute")
            and hasattr(filter_condition, "value")
        ):
            operator = operator_map.get(filter_condition.operator)
            if not operator:
                raise ValueError(f"Unsupported operator: {filter_condition.operator}")

            if operator in ["BETWEEN", "IN"]:
                # Handle special cases
                if operator == "BETWEEN":
                    return f"c.{filter_condition.attribute} BETWEEN {filter_condition.value[0]} AND {filter_condition.value[1]}"
                elif operator == "IN":
                    values = ", ".join([str(v) for v in filter_condition.value])
                    return f"c.{filter_condition.attribute} IN ({values})"
            else:
                return f"c.{filter_condition.attribute} {operator} {filter_condition.value}"

        return ""

    @classmethod
    def _cosmos_to_model(cls, item: dict, load_full_item: bool = False) -> _T:
        """
        Convert Cosmos DB item to model instance
        """
        # Remove Cosmos DB specific fields if not needed
        if not load_full_item:
            item.pop("_rid", None)
            item.pop("_self", None)
            item.pop("_etag", None)
            item.pop("_attachments", None)
            item.pop("_ts", None)

        return cls(**item)

    @classmethod
    def scan_aws(
        cls: Type[_T],
        filter_condition: Optional[ConditionBase] = None,
        *,
        consistent_read: bool = False,
        index: Optional[str] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        load_full_item: bool = False,
    ):
        while True:
            result = cls.scan_page_aws(
                filter_condition=filter_condition,
                consistent_read=consistent_read,
                index=index,
                per_page=per_page,
                last_evaluated_key=last_evaluated_key,
                load_full_item=load_full_item,
            )

            last_evaluated_key = result.last_evaluated_key
            yield from result.items

            if not result.has_more:
                break

    @classmethod
    def scan_page_aws(
        cls: Type[_T],
        filter_condition: Optional[ConditionBase] = None,
        *,
        consistent_read: bool = False,
        index: Optional[str] = None,
        per_page: Optional[int] = None,
        last_evaluated_key: Optional[dict] = None,
        load_full_item: bool = False,
    ) -> ResultPage[_T]:
        response = cls._dyntastic_call(
            "scan",
            ConsistentRead=consistent_read,
            IndexName=index,
            Limit=per_page,
            ExclusiveStartKey=last_evaluated_key,
            FilterExpression=filter_condition,
        )

        raw_items = response.get("Items")
        items = [
            cls._dyntastic_load_model(item, load_full_item=load_full_item)
            for item in raw_items
        ]
        last_evaluated_key = response.get("LastEvaluatedKey")

        return ResultPage(items, last_evaluated_key)

    def save(self, *, condition: Optional[ConditionBase] = None):
        if host_provider == HostProvider.AWS:
            return self.save_aws(condition=condition)
        elif host_provider == HostProvider.AZURE:
            return self.save_azure(condition=condition)
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    def save_azure(self, *, condition: Optional[ConditionBase] = None):
        container_client: ContainerProxy = self._get_cosmos_client()
        data = pydantic_compat.model_dump(self, by_alias=True, mode="json")
        data["id"] = data[self.__hash_key__]
        item = container_client.upsert_item(data)
        return self._cosmos_to_model(item, load_full_item=True)

    def save_aws(self, *, condition: Optional[ConditionBase] = None):
        data = pydantic_compat.model_dump(self, by_alias=True)
        dynamo_serialized = attr.serialize(data)
        return self._dyntastic_call(
            "put_item", Item=dynamo_serialized, ConditionExpression=condition
        )

    def delete_aws(self, *, condition: Optional[ConditionBase] = None):
        return self._dyntastic_call(
            "delete_item", Key=self._dyntastic_key_dict, ConditionExpression=condition
        )

    def delete_azure(self):
        container_client: ContainerProxy = self._get_cosmos_client()
        delete_args = {}
        if self.__range_key__:
            delete_args["partition_key"] = self._dyntastic_key_dict[self.__range_key__]
        else:
            delete_args["partition_key"] = self._dyntastic_key_dict[self.__hash_key__]
        delete_args["item"] = self._dyntastic_key_dict[self.__hash_key__]
        return container_client.delete_item(**delete_args)

    def delete(self, *, condition: Optional[ConditionBase] = None):
        if host_provider == HostProvider.AWS:
            return self.delete_aws(condition=condition)
        elif host_provider == HostProvider.AZURE:
            return self.delete_azure()
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")

    # TODO: Support ReturnValues
    def update(
        self,
        *actions: _UpdateAction,
        condition: Optional[ConditionBase] = None,
        require_condition: bool = False,
        refresh: bool = True,
    ):
        if not actions:
            raise ValueError("Must provide at least one action to update")

        # TODO: Run all of the expression value through pydantic validators on
        # the class, to support all of the various input type casting (do this
        # before serialize)
        update_data: Dict[str, Any] = attr.serialize(translate_updates(*actions))
        try:
            response = self._dyntastic_call(
                "update_item",
                Key=self._dyntastic_key_dict,
                ConditionExpression=condition,
                **update_data,
            )
            self._dyntastic_unrefreshed = True
            if refresh:
                if current_transaction_writer() is not None:
                    warnings.warn(
                        "Cannot refresh model in transaction, skipping refresh",
                        stacklevel=2,
                    )
                else:
                    # TODO: utilize ReturnValues in response when possible
                    self.refresh()

            return response
        except self.ConditionException():
            if require_condition:
                raise

    def refresh(self):
        self._dyntastic_unrefreshed = False
        self._dyntastic_missing_attributes_from_index = False
        if host_provider == HostProvider.AZURE:
            data = self.get_azure(self._dyntastic_hash_key, self._dyntastic_range_key)
        elif host_provider == HostProvider.AWS:
            data = self.get_aws(self._dyntastic_hash_key, self._dyntastic_range_key)
        else:
            raise NotImplementedError(f"Host provider {host_provider} not implemented")
        self.__dict__.update(data.__dict__)

    def transaction_condition(self, condition: ConditionBase):
        transaction_writer = current_transaction_writer()
        if transaction_writer is None:
            raise Exception(
                f"Cannot use {self.__class__.__name__}.transaction_condition() outside of a transaction"
            )

        item = self._construct_transact_item(
            "transaction_condition",
            {"Key": self._dyntastic_key_dict, "ConditionExpression": condition},
        )
        transaction_writer.add(self.__class__, item)

    @classmethod
    def batch_writer(cls, batch_size: int = 25):
        return BatchWriter(cls, batch_size=batch_size)

    @classmethod
    def submit_batch_write(cls, batch: List[dict]):
        if not batch:
            return

        responses = invoke_with_backoff(
            cls._dynamodb_resource().batch_write_item,
            {cls._resolve_table_name(): batch},
            "UnprocessedItems",
        )

        return responses

    # Note: This cannot use @classmethod and @property together for python <3.9
    @classmethod
    def ConditionException(cls):
        return (
            cls._dynamodb_table().meta.client.exceptions.ConditionalCheckFailedException
        )

    # TODO: support more configuration for new table
    @classmethod
    def create_table(cls, *indexes: Union[str, Index], wait: bool = True):
        """Creates a DynamoDB table (primarily for testing, limited configuration supported)"""

        throughput = {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}
        attributes = {cls.__hash_key__}
        key_schema = [{"AttributeName": cls.__hash_key__, "KeyType": "HASH"}]
        if cls.__range_key__:
            attributes.add(cls.__range_key__)
            key_schema.append({"AttributeName": cls.__range_key__, "KeyType": "RANGE"})

        kwargs = {}
        if indexes:
            secondary_indexes = []
            for index in indexes:
                if isinstance(index, str):
                    index = Index(index)

                attributes.add(index.hash_key)
                index_schema = [{"AttributeName": index.hash_key, "KeyType": "HASH"}]
                if index.range_key:
                    attributes.add(index.range_key)
                    index_schema.append(
                        {"AttributeName": index.range_key, "KeyType": "RANGE"}
                    )

                secondary_indexes.append(
                    {
                        "IndexName": index.index_name,
                        "KeySchema": index_schema,
                        "Projection": {"ProjectionType": index.projection},
                        "ProvisionedThroughput": {
                            "ReadCapacityUnits": 1,
                            "WriteCapacityUnits": 1,
                        },
                    }
                )

            kwargs["GlobalSecondaryIndexes"] = secondary_indexes

        attribute_definitions = [
            {"AttributeName": attr, "AttributeType": cls._dynamodb_type(attr)}
            for attr in attributes
        ]

        cls._dynamodb_resource().create_table(
            TableName=cls._resolve_table_name(),
            KeySchema=key_schema,
            AttributeDefinitions=attribute_definitions,
            ProvisionedThroughput=throughput,
            **kwargs,
        )

        if wait:
            cls._wait_until_exists()

    # Internal helpers

    @classmethod
    def _resolve_table_name(cls) -> str:
        if callable(cls.__table_name__):
            return cls.__table_name__()
        else:
            return cls.__table_name__

    @classmethod
    def _resolve_table_region(cls) -> Optional[str]:
        if callable(cls.__table_region__):
            return cls.__table_region__()
        else:
            return cls.__table_region__ or os.getenv("DYNTASTIC_REGION")

    @classmethod
    def _resolve_table_host(cls) -> Optional[str]:
        if callable(cls.__table_host__):
            return cls.__table_host__()
        else:
            return cls.__table_host__ or os.getenv("DYNTASTIC_HOST")

    @classmethod
    def _dynamodb_type(cls, key: str) -> str:
        # Note: pragma nocover on the following line as coverage marks the ->exit branch as
        # being missed (since we can always find a field matching the key passed in)
        python_type = next(
            pydantic_compat.annotation(field)
            for field_name, field in pydantic_compat.model_fields(cls).items()
            if pydantic_compat.alias(field_name, field) == key
        )  # pragma: nocover
        if python_type == bytes:
            return "B"
        elif python_type in (int, Decimal, float):
            return "N"
        else:
            # TODO: how to properly differentiate between types like datetime
            # which serialize to str, and other types that do not?
            # TODO: use boto3.dynamodb.types.TypeSerializer._get_dynamodb_type() as a reference
            return "S"

    @property
    def _dyntastic_hash_key(self):
        return getattr(self, self.__hash_key__)

    @property
    def _dyntastic_range_key(self):
        if self.__range_key__:
            return getattr(self, self.__range_key__)
        else:
            return None

    @property
    def _dyntastic_key_dict(self):
        key = {self.__hash_key__: self._dyntastic_hash_key}
        if self.__range_key__:
            key[self.__range_key__] = self._dyntastic_range_key

        return attr.serialize(key)

    @classmethod
    def _dynamodb_boto3_kwargs(cls):
        kwargs = {}

        region = cls._resolve_table_region()
        if region:
            kwargs["region_name"] = region

        host = cls._resolve_table_host()
        if host:
            kwargs["endpoint_url"] = host

        return kwargs

    @classmethod
    def _dynamodb_resource(cls):
        if cls._dynamodb_resource_instance is None:  # type: ignore
            kwargs = cls._dynamodb_boto3_kwargs()
            cls._dynamodb_resource_instance = boto3.resource("dynamodb", **kwargs)  # type: ignore
        return cls._dynamodb_resource_instance  # type: ignore

    @classmethod
    def _dynamodb_table(cls):
        if cls._dynamodb_table_instance is None:  # type: ignore
            cls._dynamodb_table_instance = cls._dynamodb_resource().Table(
                cls._resolve_table_name()
            )  # type: ignore
        return cls._dynamodb_table_instance  # type: ignore

    @classmethod
    def _dynamodb_client(cls):
        if cls._dynamodb_client_instance is None:  # type: ignore
            kwargs = cls._dynamodb_boto3_kwargs()
            cls._dynamodb_client_instance = boto3.client("dynamodb", **kwargs)  # type: ignore
        return cls._dynamodb_client_instance  # type: ignore

    @classmethod
    def _wait_until_exists(cls):
        # wait a maximum of 15 * 2 = 30 seconds
        for _ in range(15):  # pragma: no cover
            response = cls._dynamodb_client().describe_table(
                TableName=cls._resolve_table_name()
            )
            if response["Table"].get("TableStatus") == "ACTIVE":  # pragma: no cover
                break

            time.sleep(2)

    @classmethod
    def _clear_boto3_state(cls):
        cls._dynamodb_table_instance = None  # type: ignore
        cls._dynamodb_resource_instance = None  # type: ignore
        cls._dynamodb_client_instance = None  # type: ignore

    @classmethod
    def _construct_batch_item(cls, operation: str, filtered_kwargs: Dict[str, Any]):
        if operation == "delete_item":
            method = "delete"
            key = "DeleteRequest"
            required_kwargs = {"Key"}
        elif operation == "put_item":
            method = "save"
            key = "PutRequest"
            required_kwargs = {"Item"}
        else:  # pragma: nocover
            raise ValueError(
                f"Operation {operation} not supported with {cls.__name__}.batch_writer()"
            )

        if filtered_kwargs.keys() != required_kwargs:
            raise ValueError(
                f"Cannot provide additional arguments to {cls.__name__}.{method}() when using batch_writer()"
            )

        return {key: filtered_kwargs}

    @classmethod
    def _construct_transact_item(cls, operation: str, filtered_kwargs: Dict[str, Any]):
        filtered_kwargs["TableName"] = cls._resolve_table_name()

        if "ConditionExpression" in filtered_kwargs:
            condition_data = transact.serialize_condition(
                filtered_kwargs["ConditionExpression"]
            )
            filtered_kwargs["ConditionExpression"] = condition_data[
                "ConditionExpression"
            ]

            # Merging condition expression and update expression names/values so they are both present.
            # boto3 names/values look like '#n...' and ':v...', while dyntastic uses just '#...' and ':...'
            # so they should be mutually exclusive and not overlap at all
            names = filtered_kwargs.setdefault("ExpressionAttributeNames", {})
            names.update(condition_data["ExpressionAttributeNames"])

            values = filtered_kwargs.setdefault("ExpressionAttributeValues", {})
            values.update(condition_data["ExpressionAttributeValues"])

        for data_key in ("Key", "Item", "ExpressionAttributeValues"):
            if data_key in filtered_kwargs:
                filtered_kwargs[data_key] = transact.serialize_data(
                    filtered_kwargs[data_key]
                )

        key = {
            "delete_item": "Delete",
            "put_item": "Put",
            "update_item": "Update",
            "transaction_condition": "ConditionCheck",
        }.get(operation)

        if key is None:  # pragma: nocover
            raise ValueError(
                f"Operation {operation} not supported with dyntastic.TransactionWriter"
            )

        return {key: filtered_kwargs}

    @classmethod
    def _cosmos_call(cls, operation: str, **kwargs):
        pass

    @classmethod
    def _dyntastic_call(cls, operation: str, **kwargs):
        method = getattr(cls._dynamodb_table(), operation)
        filtered_kwargs = {
            key: value for key, value in kwargs.items() if value is not None
        }

        batch_writer = cls._dyntastic_batch_writer.get()
        transaction_writer = current_transaction_writer()

        if batch_writer is not None and transaction_writer is not None:
            raise ValueError(
                "Cannot use batch_writer() and transaction() at the same time"
            )

        if (batch_writer is None and transaction_writer is None) or operation in [
            "query",
            "scan",
        ]:
            return method(**filtered_kwargs)

        if batch_writer is not None:
            batch_item = cls._construct_batch_item(operation, filtered_kwargs)
            batch_writer.add(batch_item)
        elif transaction_writer is not None:
            item = cls._construct_transact_item(operation, filtered_kwargs)
            transaction_writer.add(cls, item)
        else:  # pragma: nocover
            raise Exception(
                "Logically will always have a batch or transaction writer here"
            )

    def ignore_unrefreshed(self):
        self._dyntastic_unrefreshed = False

    def _get_private_field(self, attr: str):
        try:
            return getattr(self, attr)
        except AttributeError:  # pragma: nocover
            # Note: Without this remapping AttributeError -> Exception, it is
            # particularly difficult to debug the issues that arise. For
            # example, without "model_post_init" in the __getattribute__
            # function below, the error appears as all model fields raising
            # AttributeError on access due to private fields like
            # _dyntastic_unrefreshed triggering that during pydantic's
            # __getattr__.
            #
            # Long story short, this should catch bugs in a much more easy-to-debug way.

            raise Exception(f"{attr} could not be accessed, dyntastic<->pydantic bug")

    def __getattribute__(self, attr: str):
        # breakpoint()
        # All of the code in this function works to "disable" an instance
        # that has been updated with refresh=False, to avoid accidentally
        # working with stale data

        if attr.startswith("_") or attr in {
            "refresh",
            "ignore_unrefreshed",
            "ConditionException",
            # Note: Without model_post_init here, _dyntastic_unrefreshed will
            # be accessed below before pydantic v2 is fully initialized,
            # which causes a bad state (for example, no field attribute can be accessed on the class)
            "model_post_init",
        }:
            return super().__getattribute__(attr)

        if self._get_private_field("_dyntastic_unrefreshed"):
            raise ValueError(
                "Dyntastic instance was not refreshed after update. "
                "Call refresh(), or use ignore_unrefreshed() to ignore safety checks"
            )

        try:
            return super().__getattribute__(attr)
        except AttributeError:
            if self._get_private_field("_dyntastic_missing_attributes_from_index"):
                raise ValueError(
                    "Dyntastic instance was loaded from a KEYS_ONLY or INCLUDE index. "
                    "Call refresh() to load the full item, or pass load_full_item=True to query() or scan()"
                )
            raise

    def __init_subclass__(cls, **kwargs):
        # Note: in pydantic v2, our private attributes like __hash_key__ are
        # not exposed on the model until the class is fully initialized, at
        # which point __pydantic_init_subclass__ is called.
        if pydantic_compat.IS_VERSION_1:  # pragma: nocover
            cls.__pydantic_init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs):
        if not pydantic_compat.IS_VERSION_1:  # pragma: nocover
            super().__pydantic_init_subclass__(**kwargs)  # type: ignore[unused-ignore, misc]

        cls._clear_boto3_state()

        cls._dyntastic_batch_writer = ContextVar("dyntastic_batch_writer", default=None)

        if not hasattr(cls, "__table_name__"):
            raise ValueError("Dyntastic table must have __table_name__ defined")

        if not hasattr(cls, "__hash_key__"):
            raise ValueError("Dyntastic table must have __hash_key__ defined")

        if not _has_alias(cls, cls.__hash_key__):
            raise ValueError(
                f"Dyntastic __hash_key__ is not defined as a field: '{cls.__hash_key__}'"
            )

        if cls.__range_key__ and not _has_alias(cls, cls.__range_key__):
            raise ValueError(
                f"Dyntastic __range_key__ is not defined as a field: '{cls.__range_key__}'"
            )

        all_aliases = set()
        for field_name, field in pydantic_compat.model_fields(cls).items():
            field_identifier = pydantic_compat.alias(field_name, field)
            if field_identifier in all_aliases:
                raise ValueError(
                    f"Duplicate alias '{field_identifier}' found in {cls.__name__}"
                )
            all_aliases.add(field_identifier)


def _has_alias(model: Type[BaseModel], name: str) -> bool:
    for field_name, field in pydantic_compat.model_fields(model).items():
        if pydantic_compat.alias(field_name, field) == name:
            return True

    return False
