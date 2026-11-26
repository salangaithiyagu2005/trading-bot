from __future__ import annotations

import math
import os
import time
from decimal import Decimal
from typing import Any


class DynamoDBStore:
    def __init__(self, table_name: str | None = None, region_name: str | None = None):
        import boto3

        self.table_name = table_name or os.environ.get("DYNAMODB_TABLE", "nsebot")
        self.region_name = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        self._table = boto3.resource(
            "dynamodb",
            region_name=self.region_name,
        ).Table(self.table_name)

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"pk": pk, "sk": sk},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return self._clean(item) if item else None

    def put(self, pk: str, sk: str, values: dict[str, Any], if_absent: bool = False) -> None:
        item = self._normalize({"pk": pk, "sk": sk, **values})
        request = {"Item": item}
        if if_absent:
            request["ConditionExpression"] = "attribute_not_exists(pk)"
        self._table.put_item(**request)

    def update(self, pk: str, sk: str, values: dict[str, Any]) -> None:
        if not values:
            return
        names = {f"#field{index}": key for index, key in enumerate(values)}
        parameters = {f":value{index}": value for index, value in enumerate(values.values())}
        assignments = ", ".join(
            f"#field{index} = :value{index}" for index in range(len(values))
        )
        self._table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=f"SET {assignments}",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=self._normalize(parameters),
            ConditionExpression="attribute_exists(pk)",
        )

    def query(self, pk: str, descending: bool = False) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        return self._query(Key("pk").eq(pk), descending=descending)

    def query_index(self, index_name: str, pk: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        return self._query(
            Key("gsi1pk").eq(pk),
            index_name=index_name,
        )

    def _query(self, key_condition, descending: bool = False, index_name: str | None = None):
        items = []
        request = {
            "KeyConditionExpression": key_condition,
            "ScanIndexForward": not descending,
        }
        if index_name:
            request["IndexName"] = index_name
        else:
            request["ConsistentRead"] = True
        while True:
            response = self._table.query(**request)
            items.extend(self._clean(item) for item in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            request["ExclusiveStartKey"] = last_key

    def scan(self) -> list[dict[str, Any]]:
        items = []
        request: dict[str, Any] = {"ConsistentRead": True}
        while True:
            response = self._table.scan(**request)
            items.extend(self._clean(item) for item in response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            request["ExclusiveStartKey"] = last_key

    def scan_prefix(self, prefix: str) -> list[dict[str, Any]]:
        items = []
        request: dict[str, Any] = {"ConsistentRead": True}
        while True:
            response = self._table.scan(**request)
            items.extend(
                self._clean(item)
                for item in response.get("Items", [])
                if item.get("pk", "").startswith(prefix)
            )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            request["ExclusiveStartKey"] = last_key

    def next_id(self, name: str) -> int:
        response = self._table.update_item(
            Key={"pk": f"COUNTER#{name}", "sk": "VALUE"},
            UpdateExpression="ADD #value :one",
            ExpressionAttributeNames={"#value": "value"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["value"])

    def set_counter(self, name: str, value: int) -> None:
        self._table.put_item(Item={
            "pk": f"COUNTER#{name}",
            "sk": "VALUE",
            "value": value,
        })

    def ensure_counter_at_least(self, name: str, value: int) -> None:
        current = self.get(f"COUNTER#{name}", "VALUE")
        if current is None or int(current["value"]) < value:
            self.set_counter(name, value)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass

    @staticmethod
    def _clean(item: dict[str, Any]) -> dict[str, Any]:
        internal = {"pk", "sk", "gsi1pk", "gsi1sk"}
        return {key: value for key, value in item.items() if key not in internal}

    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("DynamoDB does not accept non-finite numeric values")
            return Decimal(str(value))
        if isinstance(value, dict):
            return {key: cls._normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._normalize(item) for item in value]
        if isinstance(value, tuple):
            return [cls._normalize(item) for item in value]
        return value


def create_table(table_name: str | None = None, region_name: str | None = None) -> None:
    import boto3
    from botocore.exceptions import ClientError

    name = table_name or os.environ.get("DYNAMODB_TABLE", "nsebot")
    region = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise ValueError("Set AWS_REGION before creating the DynamoDB table.")
    client = boto3.client("dynamodb", region_name=region)
    try:
        description = client.describe_table(TableName=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        description = None

    if description is None:
        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        _wait_for_table_and_index(client, name)
        return

    _wait_for_table_active(client, name)
    description = client.describe_table(TableName=name)
    table = description["Table"]
    key_schema = {
        (key["AttributeName"], key["KeyType"])
        for key in table.get("KeySchema", [])
    }
    if key_schema != {("pk", "HASH"), ("sk", "RANGE")}:
        raise ValueError(f"DynamoDB table {name} must use pk/sk as its partition/sort keys.")

    index = next(
        (item for item in table.get("GlobalSecondaryIndexes", []) if item.get("IndexName") == "gsi1"),
        None,
    )
    if index is not None:
        index_schema = {
            (key["AttributeName"], key["KeyType"])
            for key in index.get("KeySchema", [])
        }
        if index_schema != {("gsi1pk", "HASH"), ("gsi1sk", "RANGE")} \
                or index.get("Projection", {}).get("ProjectionType") != "ALL":
            raise ValueError(f"DynamoDB table {name} has an incompatible gsi1 index.")
    else:
        client.update_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexUpdates=[{
                "Create": {
                    "IndexName": "gsi1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            }],
        )
    _wait_for_table_and_index(client, name)


def _wait_for_table_active(client, table_name: str) -> None:
    for _ in range(120):
        table = client.describe_table(TableName=table_name)["Table"]
        if table.get("TableStatus") == "ACTIVE":
            return
        time.sleep(5)
    raise TimeoutError(f"DynamoDB table did not become active: {table_name}")


def _wait_for_table_and_index(client, table_name: str) -> None:
    for _ in range(120):
        table = client.describe_table(TableName=table_name)["Table"]
        table_active = table.get("TableStatus") == "ACTIVE"
        index_active = any(
            index.get("IndexName") == "gsi1" and index.get("IndexStatus") == "ACTIVE"
            for index in table.get("GlobalSecondaryIndexes", [])
        )
        if table_active and index_active:
            return
        time.sleep(5)
    raise TimeoutError(f"DynamoDB table/index did not become active: {table_name}")
