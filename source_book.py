"""读取资料并核对表头、行宽和基础引用。"""
import argparse
import json
from pathlib import Path


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    index = {}
    for name, table in data["tables"].items():
        fields = table["fields"]
        if not fields or len(fields) != len(set(fields)) or any(len(row) != len(fields) for row in table["rows"]):
            raise ValueError(f"字段不匹配：{name}")
        index[name] = [dict(zip(fields, row)) for row in table["rows"]]
    return index


def main():
    parser = argparse.ArgumentParser(description="查看本地交换资料的表名和行数")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps({name: len(rows) for name, rows in load(args.path).items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
