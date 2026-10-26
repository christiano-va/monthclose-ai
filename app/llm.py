"""xAI chat completions that must return JSON matching a schema."""
import json
import os
from typing import Any, Dict

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

XAI_BASE_URL = "https://api.x.ai/v1"


class LLMError(RuntimeError):
    pass


def _client():
    # type: () -> OpenAI
    key = os.getenv("XAI_API_KEY")
    if not key:
        raise LLMError("XAI_API_KEY is not set")
    return OpenAI(api_key=key, base_url=XAI_BASE_URL)


def _model_name():
    # type: () -> str
    model = os.getenv("XAI_MODEL")
    if not model:
        raise LLMError("XAI_MODEL is not set")
    return model


def _strip_fences(text):
    # type: (str) -> str
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned.strip()


def _type_ok(value, expected):
    # type: (Any, Any) -> bool
    if isinstance(expected, list):
        return any(_type_ok(value, item) for item in expected)
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def validate_json(data, schema, path="$"):
    # type: (Any, Dict[str, Any], str) -> None
    """Raise LLMError if data does not match a JSON Schema subset."""
    expected = schema.get("type")
    if expected is not None and not _type_ok(data, expected):
        raise LLMError("%s: expected %s, got %s" % (path, expected, type(data).__name__))

    if schema.get("type") == "object" or "properties" in schema:
        if not isinstance(data, dict):
            raise LLMError("%s: expected object" % path)
        required = schema.get("required") or []
        for key in required:
            if key not in data:
                raise LLMError("%s: missing required field %s" % (path, key))
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in data.items():
            if key in properties:
                validate_json(value, properties[key], path + "." + key)
            elif additional is False:
                raise LLMError("%s: unexpected field %s" % (path, key))

    if schema.get("type") == "array" or "items" in schema:
        if not isinstance(data, list):
            if schema.get("type") == "array":
                raise LLMError("%s: expected array" % path)
            return
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(data):
                validate_json(item, item_schema, "%s[%d]" % (path, index))

    enum = schema.get("enum")
    if enum is not None and data not in enum:
        raise LLMError("%s: %r not in enum" % (path, data))

    minimum = schema.get("minimum")
    if minimum is not None and isinstance(data, (int, float)) and data < minimum:
        raise LLMError("%s: %s below minimum %s" % (path, data, minimum))
    maximum = schema.get("maximum")
    if maximum is not None and isinstance(data, (int, float)) and data > maximum:
        raise LLMError("%s: %s above maximum %s" % (path, data, maximum))


def llm_json(prompt, schema):
    # type: (str, Dict[str, Any]) -> Dict[str, Any]
    """Call the xAI model and return JSON that matches `schema`, or raise."""
    client = _client()
    model = _model_name()
    system = (
        "You extract structured data. Reply with JSON only, matching the "
        "given schema. Use ISO dates YYYY-MM-DD. Express money as strings "
        "with two decimal places (example: \"184.50\"), never as floats."
    )
    user = prompt + "\n\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False, indent=2)

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }
    try:
        response = client.chat.completions.create(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "schema": schema,
                    "strict": True,
                },
            },
            **kwargs
        )
    except Exception:
        response = client.chat.completions.create(
            response_format={"type": "json_object"},
            **kwargs
        )

    content = response.choices[0].message.content
    if not content:
        raise LLMError("empty model response")
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise LLMError("model did not return JSON: %s" % exc)

    if not isinstance(data, dict):
        raise LLMError("top-level JSON must be an object")
    validate_json(data, schema)
    return data
