"""Parsing of exported relevance tables and editorial weight ranges."""

import csv
import json


def parse_ranges(raw):
    """Turn a list of "low,high" strings (or pairs) into low/high tuples."""
    if len(raw) != 3:
        raise ValueError("exactly three field ranges are required")
    lows, highs = [], []
    for item in raw:
        if isinstance(item, str):
            parts = item.replace(";", ",").split(",")
            if len(parts) != 2:
                raise ValueError("range {!r} must look like 'low,high'".format(item))
            low, high = float(parts[0]), float(parts[1])
        else:
            low, high = float(item[0]), float(item[1])
        lows.append(low)
        highs.append(high)
    return tuple(lows), tuple(highs)


def load_ranges(path):
    """Load ranges from a CSV (field,low,high) or JSON file."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("ranges", data)
        pairs = [(item[0], item[1]) for item in data]
    else:
        rows = list(csv.reader(text.splitlines()))
        header = [cell.strip().lower() for cell in rows[0]]
        body = rows[1:] if any(token in header for token in ("low", "min", "下限")) else rows
        pairs = [(float(row[-2]), float(row[-1])) for row in body if row]
    if len(pairs) != 3:
        raise ValueError("range file must contain exactly three fields")
    lows, highs = zip(*pairs)
    return tuple(lows), tuple(highs)


def load_records(path):
    """Load manuscripts from CSV or JSON.

    CSV needs a header row; the first column is the manuscript number and the
    next three columns are the per-field relevance scores.  JSON accepts a list
    of objects such as ``{"number": 1, "relevance": [0.1, 0.2, 0.3]}``.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    if text.startswith("[") or text.startswith("{"):
        return _records_from_json(text)
    return _records_from_csv(text)


def _records_from_csv(text):
    rows = list(csv.reader(text.splitlines()))
    if len(rows) < 2:
        raise ValueError("CSV export needs a header row and at least one manuscript")
    header = [cell.strip() for cell in rows[0]]
    records = []
    for row in rows[1:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) < 4:
            raise ValueError("CSV row {!r} needs a number and three scores".format(row))
        records.append({
            "number": int(float(row[0])),
            "relevance": tuple(float(cell) for cell in row[1:4]),
        })
    if not records:
        raise ValueError("no manuscript rows found in CSV")
    return records, header


def _records_from_json(text):
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("records", data.get("manuscripts", []))
    records = []
    for item in data:
        number = item.get("number", item.get("id", item.get("稿号")))
        relevance = item.get("relevance", item.get("scores", item.get("fields", item.get("relevance_scores"))))
        if relevance is None:
            relevance = [item[key] for key in ("field1", "field2", "field3")]
        records.append({
            "number": int(number),
            "relevance": tuple(float(value) for value in relevance),
        })
    return records, ["number", "field1", "field2", "field3"]
