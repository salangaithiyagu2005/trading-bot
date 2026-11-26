from __future__ import annotations

import argparse
import os

from nsebot.dynamodb import create_table


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the on-demand DynamoDB table for nsebot.")
    parser.add_argument(
        "--table",
        default=os.environ.get("DYNAMODB_TABLE", "nsebot"),
        help="DynamoDB table name (default: DYNAMODB_TABLE or nsebot)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="AWS region (default: AWS_REGION or AWS_DEFAULT_REGION)",
    )
    arguments = parser.parse_args()
    create_table(arguments.table, arguments.region)
    print(f"DynamoDB table {arguments.table} is ready.")


if __name__ == "__main__":
    main()
