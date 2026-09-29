"""Box inventory API.

Boxes live in one DynamoDB table. The current card is SK=META. Every change of
the contents is appended as SK=HIST#... Photos stay private in S3 and are shown
through short-lived signed URLs.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

logger = logging.getLogger()
logger.setLevel(logging.INFO)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
PASSWORD_SYMBOLS = "!@#%&*-_=+"
NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
ORIGINAL_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_s3 = None
_table = None
_serializer = None
_signing_key = None
_cognito = None

# DER prefix of a SHA-1 DigestInfo for PKCS#1 v1.5 signatures.
SHA1_DIGEST_INFO = bytes.fromhex("3021300906052b0e03021a05000414")


class ApiError(Exception):
    def __init__(self, status, message, **values):
        super().__init__(message)
        self.status = status
        self.message = message
        self.values = values


def handler(event, context):
    if isinstance(event, dict) and event.get("task") == "recognize":
        try:
            finish_recognition(event.get("number") or "", event.get("user") or "", event.get("thumb_key") or "", event.get("lang") or "ru")
        except Exception:
            logger.exception("background recognition failed")
        return {"ok": True}
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath") or "/"
    lang = request_language(event)
    try:
        parts = [unquote(part) for part in path.split("/") if part]
        if not parts or parts[0] != "api":
            raise ApiError(404, "Не найдено")
        result = route(method, parts[1:], event)
    except ApiError as exc:
        result = response(exc.status, {"error": translate(lang, exc.message, exc.values)})
    except json.JSONDecodeError:
        result = response(400, {"error": translate(lang, "Некорректный JSON")})
    except Exception as exc:
        if error_code(exc) == "UserNotFoundException":
            result = response(404, {"error": translate(lang, "Такого пользователя нет")})
        elif conditional_failed(exc):
            result = response(409, {"error": translate(lang, "Коробку уже изменили. Обновите карточку и повторите.")})
        else:
            logger.exception("unhandled")
            result = response(500, {"error": translate(lang, "Внутренняя ошибка")})
    if 200 <= result["statusCode"] < 300:
        attach_media_cookies(result, event)
    logger.info("%s %s -> %s", method, path, result["statusCode"])
    return result


def route(method, parts, event):
    params = event.get("queryStringParameters") or {}
    if parts == ["me"] and method == "GET":
        return response(200, {"email": actor(event), "admin": is_admin(event)})
    if parts[:1] == ["admin"]:
        return admin_route(method, parts[1:], params, event)
    if parts == ["settings"] and method == "GET":
        return get_settings(event)
    if parts == ["settings"] and method == "PUT":
        return put_settings(event)
    if parts == ["boxes"] and method == "GET":
        return list_boxes(event)
    if parts == ["boxes"] and method == "POST":
        return create_box(event)
    if len(parts) == 2 and parts[0] == "boxes" and method == "GET":
        return get_box(parts[1])
    if len(parts) == 2 and parts[0] == "boxes" and method == "PATCH":
        return update_box(parts[1], event)
    if len(parts) == 2 and parts[0] == "boxes" and method == "DELETE":
        return delete_box(parts[1])
    if len(parts) == 3 and parts[0] == "boxes" and parts[2] == "history" and method == "GET":
        return get_history(parts[1])
    if len(parts) == 3 and parts[0] == "boxes" and parts[2] == "photo" and method == "POST":
        return presign_photo(parts[1], event)
    if len(parts) == 4 and parts[0] == "boxes" and parts[2] == "photo" and parts[3] == "complete" and method == "POST":
        return complete_photo(parts[1], event)
    raise ApiError(404, "Не найдено")


def list_boxes(event):
    params = event.get("queryStringParameters") or {}
    tokens = query_tokens(params.get("q") or "")[:8]
    limit = clamp_limit(params.get("limit"))
    cursor = decode_cursor(params.get("cursor"))
    if tokens:
        items, next_key = search_boxes(tokens, cursor, limit)
    else:
        items, next_key = query_boxes(cursor, limit)
    return response(200, {
        "items": [public_card(item) for item in items],
        "next_cursor": encode_cursor(next_key),
        "urls_expire_at": urls_expire_at(),
    })


def query_boxes(cursor, limit):
    from boto3.dynamodb.conditions import Key

    kwargs = {
        "IndexName": "boxes",
        "KeyConditionExpression": Key("gsi1pk").eq("BOX"),
        "Limit": limit,
        "ScanIndexForward": True,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    resp = table().query(**kwargs)
    return resp.get("Items", []), resp.get("LastEvaluatedKey")


def search_boxes(tokens, cursor, limit):
    from boto3.dynamodb.conditions import Attr

    # Scan Limit applies before the filter, so a short page of matches is normal.
    # Every match from a fully read page is returned; the cursor never skips a
    # box that was already evaluated.
    filt = Attr("sk").eq("META")
    for token in tokens:
        filt = filt & Attr("search_text").contains(token)

    found = []
    start = cursor
    pages = 0
    last_key = None
    while pages < 25:
        kwargs = {"FilterExpression": filt, "Limit": 100}
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table().scan(**kwargs)
        pages += 1
        found.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if len(found) >= limit or not last_key:
            break
        start = last_key
    found.sort(key=lambda item: item.get("gsi1sk") or item.get("number") or "")
    return found, last_key


def create_box(event):
    payload = body_json(event)
    number = validate_number(payload.get("number"))
    description = clean_text(payload.get("description"), 4000, "Описание")
    contents = clean_text(payload.get("contents"), 20000, "Содержимое")
    now = now_iso()
    who = actor(event)
    item = {
        "pk": box_pk(number),
        "sk": "META",
        "number": number,
        "gsi1pk": "BOX",
        "gsi1sk": sort_key(number),
        "description": description,
        "contents": contents,
        "search_text": build_search(number, description, contents),
        "updated_at": now,
        "updated_by": who,
        "version": 1,
    }
    actions = [{
        "Put": {
            "TableName": table_name(),
            "Item": marshal_item(item),
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }]
    if contents:
        actions.append({"Put": {"TableName": table_name(), "Item": marshal_item(history_item(
            number, "", contents, now, who, "Создание коробки"
        ))}})
    try:
        transact(actions)
    except Exception as exc:
        if conditional_failed(exc):
            raise ApiError(409, "Коробка с таким номером уже есть") from exc
        raise
    return response(201, public_detail(item))


def get_box(number):
    meta = require_meta(number)
    return response(200, public_detail(meta))


def update_box(number, event):
    number = validate_number(number)
    payload = body_json(event)
    description = clean_text(payload.get("description"), 4000, "Описание")
    contents = clean_text(payload.get("contents"), 20000, "Содержимое")
    note = clean_text(payload.get("note"), 500, "Комментарий")
    version = parse_version(payload.get("version"))
    current = require_meta(number)
    now = now_iso()
    who = actor(event)
    actions = [{
        "Update": {
            "TableName": table_name(),
            "Key": {"pk": marshal(box_pk(number)), "sk": marshal("META")},
            "UpdateExpression": "SET description = :d, contents = :c, search_text = :s, updated_at = :u, updated_by = :b, version = :nv",
            "ConditionExpression": "version = :ov",
            "ExpressionAttributeValues": {
                ":d": marshal(description),
                ":c": marshal(contents),
                ":s": marshal(build_search(number, description, contents, current.get("hidden_text") or "")),
                ":u": marshal(now),
                ":b": marshal(who),
                ":nv": marshal(version + 1),
                ":ov": marshal(version),
            },
        }
    }]
    if contents != current.get("contents", ""):
        actions.append({"Put": {"TableName": table_name(), "Item": marshal_item(history_item(
            number, current.get("contents", ""), contents, now, who, note
        ))}})
    transact(actions)
    updated = dict(current)
    updated.update({
        "description": description,
        "contents": contents,
        "updated_at": now,
        "updated_by": who,
        "version": version + 1,
    })
    return response(200, public_detail(updated))


def delete_box(number):
    number = validate_number(number)
    items = query_all(number)
    if not any(item.get("sk") == "META" for item in items):
        raise ApiError(404, "Коробка не найдена")
    with table().batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
    cleanup_prefix(number, set())
    return response(200, {"deleted": True})


def get_history(number):
    from boto3.dynamodb.conditions import Key

    number = validate_number(number)
    require_meta(number)
    resp = table().query(
        KeyConditionExpression=Key("pk").eq(box_pk(number)) & Key("sk").begins_with("HIST#"),
        ScanIndexForward=False,
        Limit=100,
    )
    items = [{
        "at": item.get("changed_at"),
        "by": item.get("changed_by") or "",
        "before": item.get("contents_before") or "",
        "after": item.get("contents_after") or "",
        "note": item.get("note") or "",
    } for item in resp.get("Items", [])]
    return response(200, {"items": items})


def presign_photo(number, event):
    number = validate_number(number)
    require_meta(number)
    payload = body_json(event)
    content_type = payload.get("content_type")
    if content_type not in ORIGINAL_TYPES:
        raise ApiError(400, "Нужна фотография JPEG, PNG или WebP")
    token = uuid.uuid4().hex
    original_key = f"boxes/{number}/original/{token}.{ORIGINAL_TYPES[content_type]}"
    thumb_key = f"boxes/{number}/thumb/{token}.jpg"
    return response(200, {
        "original": presign_post(original_key, content_type, photo_max_bytes()),
        "thumb": presign_post(thumb_key, "image/jpeg", min(photo_max_bytes(), 8 * 1024 * 1024)),
    })


def complete_photo(number, event):
    number = validate_number(number)
    current = require_meta(number)
    payload = body_json(event)
    original_key = payload.get("original_key") or ""
    thumb_key = payload.get("thumb_key") or ""
    if not key_allowed(number, original_key, "original"):
        raise ApiError(400, "Некорректный файл оригинала")
    if not key_allowed(number, thumb_key, "thumb"):
        raise ApiError(400, "Некорректный файл превью")
    head = head_image(original_key, photo_max_bytes())
    head_image(thumb_key, 8 * 1024 * 1024)
    now = now_iso()
    version = as_int(current.get("version"), 1)
    who = actor(event)
    pending = recognition_configured(who)
    values = {
        ":p": original_key,
        ":t": thumb_key,
        ":c": head.get("ContentType") or "image/jpeg",
        ":s": build_search(number, current.get("description") or "", current.get("contents") or "", ""),
        ":h": "",
        ":u": now,
        ":b": who,
        ":nv": version + 1,
        ":ov": version,
    }
    expression = "SET photo_key = :p, thumb_key = :t, photo_content_type = :c, search_text = :s, hidden_text = :h, updated_at = :u, updated_by = :b, version = :nv"
    try:
        table().update_item(
            Key={"pk": box_pk(number), "sk": "META"},
            UpdateExpression=expression,
            ConditionExpression="version = :ov",
            ExpressionAttributeValues=values,
        )
    except Exception as exc:
        if conditional_failed(exc):
            raise ApiError(409, "Коробку уже изменили. Повторите загрузку фотографии.") from exc
        raise
    cleanup_prefix(number, {original_key, thumb_key})
    if pending:
        try:
            queue_recognition(number, who, thumb_key, request_language(event))
        except Exception:
            logger.exception("queue recognition failed")
            pending = False
    updated = dict(current)
    updated.update({
        "photo_key": original_key,
        "thumb_key": thumb_key,
        "photo_content_type": head.get("ContentType") or "image/jpeg",
        "hidden_text": "",
        "updated_at": now,
        "updated_by": who,
        "version": version + 1,
    })
    body = public_detail(updated)
    body["recognition_pending"] = pending
    return response(200, body)


# Recognition sends photos to Grok with the user's own API key. A public demo
# turns it off: everyone signs in as the same admin and would see the key.
def recognition_enabled():
    return os.environ.get("RECOGNITION", "true") == "true"


def recognition_configured(user_email):
    if not recognition_enabled():
        return False
    settings = table().get_item(Key={"pk": f"USER#{user_email}", "sk": "SETTINGS"}).get("Item") or {}
    return bool((settings.get("grok_api_key") or "").strip() and (settings.get("grok_model") or "").strip())


def queue_recognition(number, user_email, thumb_key, lang="ru"):
    import boto3

    boto3.client("lambda").invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({
            "task": "recognize",
            "number": number,
            "user": user_email,
            "thumb_key": thumb_key,
            "lang": lang,
        }).encode("utf-8"),
    )


def finish_recognition(number, user_email, thumb_key, lang="ru"):
    if not recognition_enabled():
        return
    number = validate_number(number)
    current = get_meta(number)
    if not current or current.get("thumb_key") != thumb_key:
        return
    hidden = describe_photo(user_email, thumb_key, lang)
    table().update_item(
        Key={"pk": box_pk(number), "sk": "META"},
        UpdateExpression="SET hidden_text = :h, search_text = :s",
        ConditionExpression="thumb_key = :t",
        ExpressionAttributeValues={
            ":h": hidden,
            ":s": build_search(number, current.get("description") or "", current.get("contents") or "", hidden),
            ":t": thumb_key,
        },
    )


def require_meta(number):
    number = validate_number(number)
    meta = get_meta(number)
    if not meta:
        raise ApiError(404, "Коробка не найдена")
    return meta


def get_meta(number):
    resp = table().get_item(Key={"pk": box_pk(number), "sk": "META"})
    return resp.get("Item")


def query_all(number):
    from boto3.dynamodb.conditions import Key

    items = []
    start = None
    while True:
        kwargs = {"KeyConditionExpression": Key("pk").eq(box_pk(number))}
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table().query(**kwargs)
        items.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            return items


def history_item(number, before, after, now, who, note):
    return {
        "pk": box_pk(number),
        "sk": f"HIST#{now}#{uuid.uuid4().hex[:8]}",
        "contents_before": before,
        "contents_after": after,
        "changed_at": now,
        "changed_by": who,
        "note": note,
    }


def presign_post(key, content_type, max_bytes):
    post = s3().generate_presigned_post(
        Bucket=media_bucket(),
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, max_bytes],
        ],
        ExpiresIn=300,
    )
    post["key"] = key
    return post


def head_image(key, max_bytes):
    from botocore.exceptions import ClientError

    try:
        head = s3().head_object(Bucket=media_bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ApiError(400, "Файл ещё не загружен") from exc
        raise
    size = head.get("ContentLength") or 0
    content_type = head.get("ContentType") or ""
    if size < 1 or size > max_bytes or not content_type.startswith("image/"):
        raise ApiError(400, "Файл не похож на фотографию нужного размера")
    return head


def cleanup_prefix(number, keep):
    prefix = f"boxes/{number}/"
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=media_bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"] not in keep:
                s3().delete_object(Bucket=media_bucket(), Key=obj["Key"])


def key_allowed(number, key, kind):
    prefix = f"boxes/{number}/{kind}/"
    if not isinstance(key, str) or not key.startswith(prefix) or ".." in key:
        return False
    suffix = "jpg" if kind == "thumb" else "jpg|png|webp"
    return re.fullmatch(rf"[a-f0-9]{{32}}\.(?:{suffix})", key[len(prefix):]) is not None


def public_card(item):
    thumb_key = item.get("thumb_key") or ""
    photo_key = item.get("photo_key") or ""
    return {
        "number": item.get("number") or "",
        "description": item.get("description") or "",
        "contents": item.get("contents") or "",
        "contents_preview": preview(item.get("contents") or ""),
        "has_photo": bool(photo_key),
        "thumb_url": signed_get(thumb_key or photo_key),
        "photo_url": signed_get(photo_key),
        "updated_at": item.get("updated_at") or "",
        "updated_by": item.get("updated_by") or "",
        "version": as_int(item.get("version"), 1),
    }


def public_detail(item):
    card = public_card(item)
    card["contents"] = item.get("contents") or ""
    card["hidden_text"] = item.get("hidden_text") or ""
    card["urls_expire_at"] = urls_expire_at()
    return card


def signed_get(key):
    if not key:
        return None
    return "/media/" + key.lstrip("/")


def transact(actions):
    import boto3

    boto3.client("dynamodb").transact_write_items(TransactItems=actions)


def body_json(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded") and raw:
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return {}
    if len(raw) > 100_000:
        raise ApiError(413, "Слишком большой запрос")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ApiError(400, "Ожидался объект JSON")
    return value


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }


# The email in the ID token names the user's settings (with the Grok key) and
# signs the history. Only a verified email counts: an unverified one could be
# any address.
def actor(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    email = str(claims.get("email") or "").lower()
    if not email or str(claims.get("email_verified")).lower() != "true":
        raise ApiError(403, "Почта не подтверждена")
    return email


def validate_number(value):
    if not isinstance(value, str) or not NUMBER_RE.fullmatch(value):
        raise ApiError(400, "Номер: буквы, цифры, точка, подчёркивание и дефис, до 41 символа")
    return value


def clean_text(value, limit, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "{field} должно быть текстом", field=field)
    value = value.replace("\r\n", "\n").strip()
    if len(value) > limit:
        raise ApiError(400, "{field} длиннее {limit} символов", field=field, limit=limit)
    return value


def parse_version(value):
    try:
        version = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Не передана версия карточки") from None
    if version < 1:
        raise ApiError(400, "Некорректная версия")
    return version


def fold(text):
    text = (text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def build_search(number, description, contents, hidden=""):
    return fold(f"{number}\n{description}\n{contents}\n{hidden}")


# Letters of any alphabet count, so "étagère" or "scatola" stay whole words.
def query_tokens(text):
    return re.findall(r"[\w.-]{1,40}", fold(text))


def sort_key(number):
    if number.isdigit():
        return f"n:{int(number):012d}"
    return "s:" + number.lower()


def box_pk(number):
    return f"BOX#{number}"


def preview(text, limit=160):
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def clamp_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 60
    return max(1, min(limit, 100))


def encode_cursor(key):
    if not key:
        return None
    raw = json.dumps(key, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value):
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 2000:
        raise ApiError(400, "Некорректный курсор")
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        raise ApiError(400, "Некорректный курсор") from None
    if not isinstance(data, dict):
        raise ApiError(400, "Некорректный курсор")
    return data


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def urls_expire_at():
    return (datetime.now(timezone.utc) + timedelta(seconds=url_ttl())).isoformat()


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def conditional_failed(exc):
    response_data = getattr(exc, "response", None)
    if not isinstance(response_data, dict):
        return False
    code = response_data.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = response_data.get("CancellationReasons", [])
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


# The hidden text is searched with the words people type, so it is written in
# the language of whoever uploaded the photo.
RECOGNITION_PROMPTS = {
    "ru": (
        "Перечисли, что изображено на фотографии, для поиска по складу. "
        "Пиши по-русски, коротко: предметы, материалы, цвета, количество и читаемые надписи. "
        "Без вступления и без markdown."
    ),
    "en": (
        "List what is in the photo, for searching a storage inventory. "
        "Write in English, briefly: objects, materials, colours, quantities and readable labels. "
        "No introduction and no markdown."
    ),
    "fr": (
        "Énumère ce que montre la photo, pour la recherche dans un inventaire de stockage. "
        "Écris en français, brièvement : objets, matériaux, couleurs, quantités et inscriptions lisibles. "
        "Sans introduction et sans markdown."
    ),
    "it": (
        "Elenca cosa c’è nella foto, per la ricerca in un inventario di magazzino. "
        "Scrivi in italiano, in breve: oggetti, materiali, colori, quantità e scritte leggibili. "
        "Senza introduzione e senza markdown."
    ),
}


class RecognitionSkipped(Exception):
    pass


def get_settings(event):
    if not recognition_enabled():
        actor(event)
        return response(200, {"recognition": False, "grok_api_key": "", "grok_model": ""})
    item = table().get_item(Key={"pk": user_pk(event), "sk": "SETTINGS"}).get("Item") or {}
    return response(200, {
        "recognition": True,
        "grok_api_key": item.get("grok_api_key") or "",
        "grok_model": item.get("grok_model") or "grok-4.7",
    })


def put_settings(event):
    if not recognition_enabled():
        raise ApiError(403, "Распознавание фотографий выключено")
    payload = body_json(event)
    api_key = clean_text(payload.get("grok_api_key"), 500, "API-ключ")
    model = clean_text(payload.get("grok_model"), 80, "Модель") or "grok-4.7"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", model):
        raise ApiError(400, "Некорректное название модели")
    table().put_item(Item={
        "pk": user_pk(event),
        "sk": "SETTINGS",
        "grok_api_key": api_key,
        "grok_model": model,
        "updated_at": now_iso(),
    })
    return response(200, {"saved": True})


def user_pk(event):
    return "USER#" + actor(event)


# Users --------------------------------------------------------------------
# Members of the Cognito admin group invite, disable and delete users and
# reset two-step sign-in for someone who lost their phone.

def admin_route(method, parts, params, event):
    if not is_admin(event):
        raise ApiError(403, "Нужны права администратора")
    if parts == ["users"] and method == "GET":
        return list_users(event)
    if parts == ["users"] and method == "POST":
        return create_user(event)
    if parts == ["users"] and method == "PUT":
        return update_user(event)
    if parts == ["users"] and method == "DELETE":
        return delete_user(params.get("email"), event)
    raise ApiError(404, "Не найдено")


def is_admin(event):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        # API Gateway passes a list claim as a string such as "[admins other]".
        groups = groups.strip("[]").replace(",", " ").split()
    return admin_group() in groups


def list_users(event):
    pool = env("USER_POOL_ID")
    admins = set()
    for page in cognito().get_paginator("list_users_in_group").paginate(UserPoolId=pool, GroupName=admin_group()):
        admins.update(user_email(user) for user in page.get("Users", []))
    users = []
    for page in cognito().get_paginator("list_users").paginate(UserPoolId=pool):
        for user in page.get("Users", []):
            email = user_email(user)
            users.append({
                "email": email,
                "status": user.get("UserStatus", ""),
                "enabled": bool(user.get("Enabled", True)),
                "created": iso(user.get("UserCreateDate")),
                "admin": email in admins,
                "mfa": has_mfa(pool, user.get("Username")),
                "self": email == actor(event),
            })
    users.sort(key=lambda user: user["email"])
    return response(200, {"items": users})


# ListUsers does not say whether a user has an authenticator app, so each user
# is asked for separately. The inventory has a handful of users.
def has_mfa(pool, username):
    user = cognito().admin_get_user(UserPoolId=pool, Username=username)
    return "SOFTWARE_TOKEN_MFA" in (user.get("UserMFASettingList") or [])


def create_user(event):
    body = body_json(event)
    email = validate_email(body.get("email"))
    password = temporary_password()
    emailed = os.environ.get("INVITE_EMAILS", "true") == "true"
    # A public demo does not send mail: anyone could otherwise make it email
    # strangers. The temporary password is shown to the administrator instead.
    delivery = {"DesiredDeliveryMediums": ["EMAIL"]} if emailed else {"MessageAction": "SUPPRESS"}
    try:
        cognito().admin_create_user(
            UserPoolId=env("USER_POOL_ID"),
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=password,
            **delivery,
        )
    except Exception as exc:
        if error_code(exc) == "UsernameExistsException":
            raise ApiError(409, "Такой пользователь уже есть") from exc
        raise
    if body.get("admin"):
        cognito().admin_add_user_to_group(UserPoolId=env("USER_POOL_ID"), Username=email, GroupName=admin_group())
    return response(200, {"email": email, "temporary_password": password, "emailed": emailed})


def update_user(event):
    body = body_json(event)
    email = validate_email(body.get("email"))
    pool = env("USER_POOL_ID")
    own = email == actor(event)
    result = {"email": email}
    if "enabled" in body:
        if own and not body["enabled"]:
            raise ApiError(400, "Нельзя отключить самого себя")
        if body["enabled"]:
            cognito().admin_enable_user(UserPoolId=pool, Username=email)
        else:
            cognito().admin_disable_user(UserPoolId=pool, Username=email)
    if "admin" in body:
        if own and not body["admin"]:
            raise ApiError(400, "Нельзя снять права администратора с самого себя")
        if body["admin"]:
            cognito().admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=admin_group())
        else:
            cognito().admin_remove_user_from_group(UserPoolId=pool, Username=email, GroupName=admin_group())
    # A user who lost their phone signs in with the password alone and can set
    # up the app again in the settings.
    if body.get("reset_mfa"):
        cognito().admin_set_user_mfa_preference(
            UserPoolId=pool,
            Username=email,
            SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False},
        )
    if body.get("reset_password"):
        password = temporary_password()
        cognito().admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=False)
        result["temporary_password"] = password
    return response(200, result)


def delete_user(email, event):
    email = validate_email(email)
    if email == actor(event):
        raise ApiError(400, "Нельзя удалить самого себя")
    cognito().admin_delete_user(UserPoolId=env("USER_POOL_ID"), Username=email)
    # The user's settings (with the Grok key) go too; boxes and history stay.
    from boto3.dynamodb.conditions import Key

    resp = table().query(KeyConditionExpression=Key("pk").eq("USER#" + email), ProjectionExpression="pk, sk")
    for item in resp.get("Items", []):
        table().delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
    return response(200, {"deleted": email})


def validate_email(value):
    email = str(value or "").strip().lower()
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise ApiError(400, "Нужен адрес почты")
    return email


def temporary_password():
    alphabet = string.ascii_letters + string.digits + PASSWORD_SYMBOLS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(16))
        if (any(c.islower() for c in password) and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password) and any(c in PASSWORD_SYMBOLS for c in password)):
            return password


def user_email(user):
    for attribute in user.get("Attributes", []):
        if attribute.get("Name") == "email":
            return attribute.get("Value", "").lower()
    return user.get("Username", "")


def admin_group():
    return os.environ.get("ADMIN_GROUP", "admins")


def iso(value):
    if not value:
        return ""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def error_code(exc):
    data = getattr(exc, "response", None)
    return data.get("Error", {}).get("Code", "") if isinstance(data, dict) else ""


def describe_photo(user_email, thumb_key, lang="ru"):
    settings = table().get_item(Key={"pk": f"USER#{user_email}", "sk": "SETTINGS"}).get("Item") or {}
    api_key = (settings.get("grok_api_key") or "").strip()
    model = (settings.get("grok_model") or "").strip()
    if not api_key or not model:
        raise RecognitionSkipped()
    raw = s3().get_object(Bucket=media_bucket(), Key=thumb_key)["Body"].read()
    if len(raw) > 8 * 1024 * 1024:
        raise ApiError(400, "Превью слишком большое для распознавания")
    payload = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                    "detail": "auto",
                },
                {
                    "type": "input_text",
                    "text": RECOGNITION_PROMPTS.get(lang, RECOGNITION_PROMPTS["ru"]),
                },
            ],
        }],
    }
    request = urllib.request.Request(
        "https://api.x.ai/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=50) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("grok status %s", exc.code)
        if exc.code in {401, 403}:
            raise ApiError(502, "Проверьте API-ключ Grok в настройках") from exc
        raise ApiError(502, "Сервис распознавания не принял фотографию") from exc
    except urllib.error.URLError as exc:
        logger.warning("grok network error")
        raise ApiError(502, "Сервис распознавания недоступен") from exc
    text = extract_response_text(data).strip()
    if not text:
        raise ApiError(502, "Сервис распознавания не вернул описание")
    return text[:4000]


def extract_response_text(data):
    chunks = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text") and part.get("type") in {"output_text", "text"}:
                chunks.append(part["text"])
    if chunks:
        return "\n".join(chunks)
    output_text = data.get("output_text")
    return output_text if isinstance(output_text, str) else ""


def viewer_host(event):
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    host = headers.get("x-viewer-host") or ""
    host = host.split(",")[0].strip().split(":")[0]
    if not host or "execute-api" in host:
        return ""
    return host


def attach_media_cookies(result, event):
    host = viewer_host(event)
    if not host:
        return
    try:
        result["cookies"] = media_cookies(host)
    except Exception:
        logger.exception("media cookie was not issued")


def media_cookies(host):
    import time

    expires = int(time.time()) + 3600
    policy = json.dumps({
        "Statement": [{
            "Resource": f"https://{host}/media/*",
            "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
        }]
    }, separators=(",", ":")).encode("utf-8")
    signature = rsa_sha1_sign(policy, signing_key())
    attributes = "Path=/media; Secure; HttpOnly; SameSite=Lax; Max-Age=3600"
    return [
        f"CloudFront-Policy={cloudfront_b64(policy)}; {attributes}",
        f"CloudFront-Signature={cloudfront_b64(signature)}; {attributes}",
        f"CloudFront-Key-Pair-Id={os.environ['CLOUDFRONT_KEY_PAIR_ID']}; {attributes}",
    ]


def cloudfront_b64(data):
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("=", "_").replace("/", "~")


def signing_key():
    global _signing_key
    if _signing_key is None:
        import boto3
        parameter = boto3.client("ssm").get_parameter(Name=os.environ["MEDIA_KEY_PARAMETER_NAME"], WithDecryption=True)
        _signing_key = load_rsa_private_key(parameter["Parameter"]["Value"])
    return _signing_key


# CloudFront wants RSASSA-PKCS1-v1_5 with SHA-1. The runtime has no crypto
# package, so the key is read from its DER form and signed with plain pow().
def der_read(data, offset):
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    return tag, data[offset:offset + length], offset + length


def der_sequence(data):
    items = []
    offset = 0
    while offset < len(data):
        tag, value, offset = der_read(data, offset)
        items.append((tag, value))
    return items


def load_rsa_private_key(pem):
    lines = [line.strip() for line in pem.strip().splitlines()]
    der = base64.b64decode("".join(line for line in lines if line and not line.startswith("-----")))
    _, body, _ = der_read(der, 0)
    items = der_sequence(body)
    if len(items) == 3 and items[2][0] == 0x04:
        # PKCS#8 wraps the PKCS#1 key in an OCTET STRING.
        _, body, _ = der_read(items[2][1], 0)
        items = der_sequence(body)
    numbers = [int.from_bytes(value, "big") for tag, value in items if tag == 0x02]
    _, n, e, d, p, q, dp, dq, qinv = numbers[:9]
    return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": dp, "dq": dq, "qinv": qinv}


def rsa_sha1_sign(message, key):
    size = (key["n"].bit_length() + 7) // 8
    digest = SHA1_DIGEST_INFO + hashlib.sha1(message).digest()
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    m = int.from_bytes(encoded, "big")
    s1 = pow(m, key["dp"], key["p"])
    s2 = pow(m, key["dq"], key["q"])
    h = (key["qinv"] * (s1 - s2)) % key["p"]
    return (s2 + h * key["q"]).to_bytes(size, "big")


def table_name():
    return env("TABLE_NAME")


def media_bucket():
    return env("MEDIA_BUCKET")


def url_ttl():
    return max(60, as_int(os.environ.get("URL_TTL_SECONDS"), 900))


def photo_max_bytes():
    return max(1, as_int(os.environ.get("PHOTO_MAX_BYTES"), 6291456))


def env(name):
    value = os.environ.get(name)
    if not value:
        raise ApiError(500, "Сервис не настроен")
    return value


def table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(table_name())
    return _table


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config

        # Sign against the regional endpoint. The global s3.amazonaws.com host
        # answers with a redirect, and a signed POST cannot follow it.
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-central-1"
        _s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def cognito():
    global _cognito
    if _cognito is None:
        import boto3

        _cognito = boto3.client("cognito-idp")
    return _cognito


def marshal(value):
    global _serializer
    if _serializer is None:
        from boto3.dynamodb.types import TypeSerializer
        _serializer = TypeSerializer()
    return _serializer.serialize(value)


def marshal_item(item):
    return {key: marshal(value) for key, value in item.items()}


# Languages ----------------------------------------------------------------
# Messages are written in Russian in the code; the page sends its language in
# X-Inventory-Lang, otherwise Accept-Language decides.

LANGUAGES = ["ru", "en", "fr", "it"]

MESSAGES = {
    "Не найдено": ("Not found", "Introuvable", "Non trovato"),
    "Некорректный JSON": ("Invalid JSON", "JSON invalide", "JSON non valido"),
    "Коробку уже изменили. Обновите карточку и повторите.": (
        "Someone has already changed this box. Reload the card and try again.",
        "Ce carton a déjà été modifié. Rechargez la fiche et réessayez.",
        "Questa scatola è già stata modificata. Ricarica la scheda e riprova.",
    ),
    "Внутренняя ошибка": ("Internal error", "Erreur interne", "Errore interno"),
    "Коробка с таким номером уже есть": ("A box with this number already exists", "Un carton porte déjà ce numéro", "Esiste già una scatola con questo numero"),
    "Коробка не найдена": ("Box not found", "Carton introuvable", "Scatola non trovata"),
    "Нужна фотография JPEG, PNG или WebP": ("A JPEG, PNG or WebP photo is needed", "Il faut une photo JPEG, PNG ou WebP", "Serve una foto JPEG, PNG o WebP"),
    "Некорректный файл оригинала": ("Invalid original file", "Fichier original invalide", "File originale non valido"),
    "Некорректный файл превью": ("Invalid preview file", "Fichier d’aperçu invalide", "File di anteprima non valido"),
    "Коробку уже изменили. Повторите загрузку фотографии.": (
        "Someone has already changed this box. Upload the photo again.",
        "Ce carton a déjà été modifié. Envoyez de nouveau la photo.",
        "Questa scatola è già stata modificata. Carica di nuovo la foto.",
    ),
    "Файл ещё не загружен": ("The file has not been uploaded yet", "Le fichier n’est pas encore envoyé", "Il file non è ancora stato caricato"),
    "Файл не похож на фотографию нужного размера": (
        "The file does not look like a photo of the right size",
        "Le fichier ne ressemble pas à une photo de la bonne taille",
        "Il file non sembra una foto della dimensione giusta",
    ),
    "Слишком большой запрос": ("The request is too large", "Requête trop volumineuse", "Richiesta troppo grande"),
    "Ожидался объект JSON": ("A JSON object was expected", "Un objet JSON était attendu", "Era atteso un oggetto JSON"),
    "Почта не подтверждена": ("The email is not verified", "L’adresse e-mail n’est pas vérifiée", "L’email non è verificata"),
    "Номер: буквы, цифры, точка, подчёркивание и дефис, до 41 символа": (
        "Number: letters, digits, dot, underscore and hyphen, up to 41 characters",
        "Numéro : lettres, chiffres, point, tiret bas et tiret, jusqu’à 41 caractères",
        "Numero: lettere, cifre, punto, trattino basso e trattino, fino a 41 caratteri",
    ),
    "{field} должно быть текстом": ("{field} must be text", "{field} doit être du texte", "{field} deve essere testo"),
    "{field} длиннее {limit} символов": (
        "{field} is longer than {limit} characters",
        "{field} dépasse {limit} caractères",
        "{field} supera {limit} caratteri",
    ),
    "Описание": ("Description", "Description", "Descrizione"),
    "Содержимое": ("Contents", "Contenu", "Contenuto"),
    "Комментарий": ("Comment", "Commentaire", "Commento"),
    "API-ключ": ("API key", "Clé API", "Chiave API"),
    "Модель": ("Model", "Modèle", "Modello"),
    "Не передана версия карточки": ("The card version is missing", "La version de la fiche manque", "Manca la versione della scheda"),
    "Некорректная версия": ("Invalid version", "Version invalide", "Versione non valida"),
    "Некорректный курсор": ("Invalid cursor", "Curseur invalide", "Cursore non valido"),
    "Некорректное название модели": ("Invalid model name", "Nom de modèle invalide", "Nome del modello non valido"),
    "Превью слишком большое для распознавания": (
        "The preview is too large for recognition",
        "L’aperçu est trop grand pour la reconnaissance",
        "L’anteprima è troppo grande per il riconoscimento",
    ),
    "Проверьте API-ключ Grok в настройках": (
        "Check the Grok API key in the settings",
        "Vérifiez la clé API Grok dans les paramètres",
        "Controlla la chiave API Grok nelle impostazioni",
    ),
    "Сервис распознавания не принял фотографию": (
        "The recognition service did not accept the photo",
        "Le service de reconnaissance n’a pas accepté la photo",
        "Il servizio di riconoscimento non ha accettato la foto",
    ),
    "Сервис распознавания недоступен": (
        "The recognition service is unavailable",
        "Le service de reconnaissance est indisponible",
        "Il servizio di riconoscimento non è disponibile",
    ),
    "Сервис распознавания не вернул описание": (
        "The recognition service returned no description",
        "Le service de reconnaissance n’a renvoyé aucune description",
        "Il servizio di riconoscimento non ha restituito una descrizione",
    ),
    "Сервис не настроен": ("The service is not configured", "Le service n’est pas configuré", "Il servizio non è configurato"),
    "Нужны права администратора": ("Administrator rights are needed", "Droits d’administrateur requis", "Servono i diritti di amministratore"),
    "Такой пользователь уже есть": ("This user already exists", "Cet utilisateur existe déjà", "Questo utente esiste già"),
    "Такого пользователя нет": ("There is no such user", "Cet utilisateur n’existe pas", "Questo utente non esiste"),
    "Нельзя отключить самого себя": ("You cannot disable yourself", "Vous ne pouvez pas vous désactiver", "Non puoi disattivare te stesso"),
    "Нельзя снять права администратора с самого себя": (
        "You cannot remove your own administrator rights",
        "Vous ne pouvez pas retirer vos propres droits d’administrateur",
        "Non puoi togliere a te stesso i diritti di amministratore",
    ),
    "Нельзя удалить самого себя": ("You cannot delete yourself", "Vous ne pouvez pas vous supprimer", "Non puoi eliminare te stesso"),
    "Нужен адрес почты": ("An email address is needed", "Une adresse e-mail est requise", "Serve un indirizzo email"),
    "Распознавание фотографий выключено": (
        "Photo recognition is turned off",
        "La reconnaissance des photos est désactivée",
        "Il riconoscimento delle foto è disattivato",
    ),
}


def request_language(event):
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    wanted = [headers.get("x-inventory-lang", "")]
    wanted += [part.split(";")[0] for part in headers.get("accept-language", "").split(",")]
    for tag in wanted:
        code = tag.strip().lower().split("-")[0]
        if code in LANGUAGES:
            return code
    return "ru"


def translate(lang, message, values=None):
    def word(text):
        if lang != "ru" and text in MESSAGES:
            return MESSAGES[text][LANGUAGES.index(lang) - 1]
        return text

    values = {key: word(value) if isinstance(value, str) else value for key, value in (values or {}).items()}
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), word(message))

