"""xAI chat completions that must return JSON matching a schema."""
import base64
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

XAI_BASE_URL = "https://api.x.ai/v1"


class LLMError(RuntimeError):
    pass


_LLM_CALLS = 0
logger = logging.getLogger("app.llm")
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "llm"
_FILE_DIGEST = ContextVar("llm_file_digest", default="")


def reset_llm_calls():
    # type: () -> None
    global _LLM_CALLS
    _LLM_CALLS = 0


def llm_call_count():
    # type: () -> int
    return _LLM_CALLS


def _client():
    # type: () -> OpenAI
    key = os.getenv("XAI_API_KEY")
    if not key:
        raise LLMError("XAI_API_KEY is not set")
    return OpenAI(api_key=key, base_url=XAI_BASE_URL, timeout=120.0)


def _model_name():
    # type: () -> str
    model = os.getenv("XAI_MODEL")
    if not model:
        raise LLMError("XAI_MODEL is not set")
    return model


def vision_model_name():
    # type: () -> str
    return os.getenv("XAI_VISION_MODEL") or "grok-4.7"


@contextmanager
def file_cache(data):
    # type: (bytes) -> Any
    """Remember LLM replies for these exact bytes. A repeat does not call the API."""
    digest = hashlib.sha256(data).hexdigest()
    if _FILE_DIGEST.get() == digest:
        yield digest
        return
    token = _FILE_DIGEST.set(digest)
    try:
        yield digest
    finally:
        _FILE_DIGEST.reset(token)


def _cache_file(model, prompt, schema, image):
    # type: (str, str, Dict[str, Any], Optional[bytes]) -> Optional[Path]
    digest = _FILE_DIGEST.get()
    if not digest:
        return None
    image_key = hashlib.sha256(image).hexdigest() if image else ""
    material = "\n".join(
        [model, prompt, image_key, json.dumps(schema, sort_keys=True, ensure_ascii=False)]
    )
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return CACHE_DIR / digest / (key + ".json")


def _read_cache(path, schema):
    # type: (Optional[Path], Dict[str, Any]) -> Optional[Dict[str, Any]]
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        validate_json(data, schema)
    except LLMError:
        return None
    logger.info("llm cache hit")
    return data


def _write_cache(path, data):
    # type: (Optional[Path], Dict[str, Any]) -> None
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


def _reasoning_rejected(exc):
    # type: (Exception) -> bool
    message = str(exc).lower()
    if "reasoning" not in message:
        return False
    code = getattr(exc, "status_code", None)
    if code is None:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    return code in (None, 400, 422)


def _complete(client, kwargs, response_format):
    # type: (Any, Dict[str, Any], Dict[str, Any]) -> Any
    try:
        return client.chat.completions.create(response_format=response_format, **kwargs)
    except Exception as exc:
        if kwargs.get("extra_body") and _reasoning_rejected(exc):
            logger.info("reasoning_effort not supported; retrying without it")
            print("scan step reasoning_effort unsupported", flush=True)
            kwargs.pop("extra_body", None)
            return _complete(client, kwargs, response_format)
        raise


def llm_json(prompt, schema, image=None, image_mime="image/jpeg", reasoning_effort=None):
    # type: (str, Dict[str, Any], Optional[bytes], str, Optional[str]) -> Dict[str, Any]
    """Call the xAI model and return JSON that matches `schema`, or raise.

    When a file cache is active, the same file and the same prompt are read
    from disk instead of calling the API again. A photo uses low reasoning
    effort when the model accepts it, so the scan spends less time waiting.
    """
    model = vision_model_name() if image else _model_name()
    if image is not None and reasoning_effort is None:
        reasoning_effort = "low"
    if image is not None:
        system = (
            "You read one document photo and reply with JSON only. "
            "Dates are YYYY-MM-DD. Money is a string with two decimals, never a float. "
            "Use null when a value is not printed."
        )
        schema_blob = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    else:
        system = (
            "You extract structured data. Reply with JSON only, matching the "
            "given schema. Use ISO dates YYYY-MM-DD. Express money as strings "
            "with two decimal places (example: \"184.50\"), never as floats."
        )
        schema_blob = json.dumps(schema, ensure_ascii=False, indent=2)
    user = prompt + "\n\nJSON schema:\n" + schema_blob
    cached = _read_cache(_cache_file(model, user, schema, image), schema)
    if cached is not None:
        if image is not None:
            logger.info("scan step vision_api cache_hit 0.000s")
            print("scan step vision_api cache_hit 0.000s", flush=True)
        return cached

    if image:
        encoded = base64.b64encode(image).decode("ascii")
        user_content = [
            {"type": "text", "text": user},
            {
                "type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (image_mime, encoded)},
            },
        ]
    else:
        user_content = user

    client = _client()
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
    }
    if reasoning_effort:
        kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}
    schema_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "extraction",
            "schema": schema,
            "strict": True,
        },
    }
    global _LLM_CALLS
    _LLM_CALLS += 1
    started = time.perf_counter()
    try:
        response = _complete(client, kwargs, schema_format)
    except Exception:
        _LLM_CALLS += 1
        response = _complete(client, kwargs, {"type": "json_object"})
    if image is not None:
        seconds = time.perf_counter() - started
        logger.info("scan step vision_api %.3fs", seconds)
        print("scan step vision_api %.3fs" % seconds, flush=True)

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
    _write_cache(_cache_file(model, user, schema, image), data)
    return data
