"""Minimal frontend v1 message patches, preserving the existing SSE consumer."""
from __future__ import annotations

import copy
import json


async def decoded_lines(response):
    decoder = FrontendDecoder()
    async for line in response.aiter_lines():
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                frame = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                for result in decoder.decode(frame):
                    yield "data: " + json.dumps(result)
            continue
        yield line


class FrontendDecoder:
    def __init__(self):
        self.state = {}
        self.last_path = None
        self.last_op = None

    def decode(self, frame: dict) -> list[dict]:
        path, op, value = frame.get("p"), frame.get("o"), frame.get("v")
        if op == "patch" and isinstance(value, list):
            result = []
            for child in value:
                if isinstance(child, dict):
                    result.extend(self.decode(child))
            return result
        if isinstance(value, dict) and "message" in value and path in (None, ""):
            self.state = copy.deepcopy(value)
            self.last_path = self.last_op = None
            return [dict(copy.deepcopy(value), conversation_id=frame.get("conversation_id") or value.get("conversation_id"))]
        if isinstance(frame.get("message"), dict):
            self.state = copy.deepcopy(frame)
            self.last_path = self.last_op = None
            return [frame]
        if path is None and isinstance(value, str) and self.last_path:
            path, op = self.last_path, op or "append"
        if isinstance(path, str) and path and op in ("append", "replace", "add", "remove", "patch"):
            self.last_path, self.last_op = path, op
            keys = [key.replace("~1", "/").replace("~0", "~") for key in path.lstrip("/").split("/")]
            node = self.state
            try:
                for i, key in enumerate(keys[:-1]):
                    factory = list if keys[i + 1].isdigit() or keys[i + 1] == "-" else dict
                    if isinstance(node, list):
                        index = len(node) if key == "-" else int(key)
                        while len(node) <= index:
                            node.append(None)
                        if not isinstance(node[index], (dict, list)):
                            node[index] = factory()
                        node = node[index]
                    else:
                        if not isinstance(node.get(key), (dict, list)):
                            node[key] = factory()
                        node = node[key]
                key = (len(node) if keys[-1] == "-" else int(keys[-1])) if isinstance(node, list) else keys[-1]
                if op == "remove":
                    if isinstance(node, list) and key < len(node):
                        del node[key]
                    elif isinstance(node, dict):
                        node.pop(key, None)
                else:
                    if isinstance(node, list):
                        while len(node) <= key:
                            node.append(None)
                        existing = node[key]
                    else:
                        existing = node.get(key)
                    if op in ("append", "patch") and isinstance(existing, dict) and isinstance(value, dict):
                        node[key] = {**existing, **value}
                    elif op == "append" and isinstance(existing, str) and isinstance(value, str):
                        node[key] = existing + value
                    elif op == "append" and isinstance(existing, list):
                        # a list value extends the list (e.g. content_references appended in batches);
                        # nesting it made later paths like content_references/0/safe_urls unreachable
                        node[key] = [*existing, *value] if isinstance(value, list) else [*existing, value]
                    else:
                        node[key] = value
            except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
                raise RuntimeError(f"unsupported frontend message patch path={path!r} op={op!r} value_type={type(value).__name__}; refusing partial output") from exc
            return [copy.deepcopy(self.state)]
        return [frame]
