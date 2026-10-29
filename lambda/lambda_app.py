"""Box inventory API.

Boxes live in one DynamoDB table. The current card is SK=META. Every change of
the contents, and of the photo description written by recognition, is appended
as SK=HIST#... Photos stay private in S3 and are shown through short-lived
signed URLs.
"""

import base64
import hashlib
import html.parser
import json
import logging
import os
import re
import secrets
import string
import time
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
    # detail is technical text for the recognition log, never shown as the error.
    def __init__(self, status, message, detail="", **values):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail
        self.values = values


def handler(event, context):
    if isinstance(event, dict) and event.get("task") == "recognize":
        try:
            finish_recognition(event.get("number") or "", event.get("user") or "", event.get("thumb_key") or "", event.get("lang") or "ru")
        except Exception:
            logger.exception("background recognition failed")
        return {"ok": True}
    if isinstance(event, dict) and event.get("task") == "recognize_all":
        try:
            recognize_all(event.get("user") or "", event.get("run") or "", event.get("numbers") or [], event.get("lang") or "ru", context)
        except Exception:
            logger.exception("recognition of all photos failed")
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
    logger.info("%s %s -> %s", method, route_label(path), result["statusCode"])
    return result


# The log names the route, not the request: box numbers, history ids and
# whatever else a caller puts in the path stay out of it. Each word comes from
# this table, never from the request.
ROUTE_WORDS = {word: word for word in (
    "api", "me", "version", "admin", "users", "settings", "recognition-log", "recognition",
    "rerun", "trash", "restore", "boxes", "history", "photo", "complete", "drafts", "new",
)}


def route_label(path):
    return "/" + "/".join(ROUTE_WORDS.get(part, "*") for part in path.split("/") if part)


def route(method, parts, event):
    params = event.get("queryStringParameters") or {}
    if parts == ["me"] and method == "GET":
        return response(200, {"email": actor(event), "admin": is_admin(event)})
    if parts == ["version"] and method == "GET":
        return response(200, version_info())
    if parts[:1] == ["admin"]:
        return admin_route(method, parts[1:], params, event)
    if parts == ["settings"] and method == "GET":
        return get_settings(event)
    if parts == ["settings"] and method == "PUT":
        return put_settings(event)
    if parts == ["settings", "recognition-log"] and method == "GET":
        return get_recognition_log(event)
    if parts == ["recognition", "rerun"] and method == "POST":
        return start_rerun(event)
    if parts == ["recognition", "rerun"] and method == "DELETE":
        return stop_rerun(event)
    if parts == ["trash"] and method == "GET":
        return list_trash()
    if len(parts) == 3 and parts[0] == "trash" and parts[2] == "restore" and method == "POST":
        return restore_box(parts[1], event)
    if len(parts) == 2 and parts[0] == "trash" and method == "DELETE":
        return purge_box(parts[1], event)
    if parts == ["boxes"] and method == "GET":
        return list_boxes(event)
    if parts == ["boxes"] and method == "POST":
        return create_box(event)
    if len(parts) == 2 and parts[0] == "boxes" and method == "GET":
        return get_box(parts[1])
    if len(parts) == 2 and parts[0] == "boxes" and method == "PATCH":
        return update_box(parts[1], event)
    if len(parts) == 2 and parts[0] == "boxes" and method == "DELETE":
        return delete_box(parts[1], event)
    if len(parts) == 3 and parts[0] == "boxes" and parts[2] == "history" and method == "GET":
        return get_history(parts[1])
    if len(parts) == 4 and parts[0] == "boxes" and parts[2] == "history" and method == "DELETE":
        return delete_history_entry(parts[1], parts[3], event)
    if parts == ["drafts"] and method == "GET":
        return list_drafts(event)
    if parts == ["drafts", "new"] and method in {"PUT", "DELETE"}:
        return put_draft(None, event) if method == "PUT" else delete_draft(None, event)
    if len(parts) == 3 and parts[:2] == ["drafts", "boxes"] and method in {"PUT", "DELETE"}:
        return put_draft(parts[2], event) if method == "PUT" else delete_draft(parts[2], event)
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
    sort = params.get("sort") if params.get("sort") in LIST_SORTS else "number"
    items, next_key = sorted_page(tokens, params.get("history") == "1", sort, cursor, limit)
    return response(200, {
        "items": [dict(public_card(item), **({"history_match": True} if item.get("history_match") else {})) for item in items],
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


# Every matching card is read, sorted here and cut into pages by position: an
# inventory holds a few hundred boxes. By number (the box address, /box/<number>)
# is the default, with numbers inside compared as numbers; then by name and the
# newest first by date.
DATE_SORTS = {"updated": "updated_at", "created": "created_at"}
LIST_SORTS = {"number", "name", *DATE_SORTS}


# "A-9" before "A-10", "12" before "12a" before "13", plain numbers first.
def natural_key(number):
    parts = [part for part in re.split(r"(\d+)", (number or "").lower()) if part]
    return [(0, int(part), part) if part.isdigit() else (1, 0, part) for part in parts]


def sorted_page(tokens, history, sort, cursor, limit):
    if tokens and history:
        fetch = lambda start: search_with_history(tokens, start, 1000)
    elif tokens:
        fetch = lambda start: search_boxes(tokens, start, 1000)
    else:
        fetch = lambda start: query_boxes(start, 1000)
    # A box found by its history on one pass and by its card on another counts
    # once, as a card match.
    found = {}
    start = None
    while True:
        page, start = fetch(start)
        for item in page:
            if item["pk"] not in found or found[item["pk"]].get("history_match"):
                found[item["pk"]] = item
        if not start:
            break
    items = list(found.values())
    if sort == "created":
        fill_created(items)
    items.sort(key=lambda item: natural_key(item.get("number")))
    if sort == "name":
        # Boxes without a description go last, by number.
        items.sort(key=lambda item: (not plain_title(item.get("description")), fold(plain_title(item.get("description")))))
    elif sort in DATE_SORTS:
        items.sort(key=lambda item: item.get(DATE_SORTS[sort]) or "", reverse=True)
    offset = max(0, as_int((cursor or {}).get("offset")))
    rest = items[offset:offset + limit]
    more = offset + limit < len(items)
    return rest, {"offset": offset + limit} if more else None


# Boxes from before created_at existed get the time of their first history
# entry (usually "box created"), otherwise of their last change, saved once.
def fill_created(items):
    from boto3.dynamodb.conditions import Key

    for item in items:
        if item.get("created_at"):
            continue
        first = table().query(
            KeyConditionExpression=Key("pk").eq(item["pk"]) & Key("sk").begins_with("HIST#"),
            ScanIndexForward=True,
            Limit=1,
        ).get("Items", [])
        item["created_at"] = (first[0].get("changed_at") if first else "") or item.get("updated_at") or ""
        try:
            table().update_item(
                Key={"pk": item["pk"], "sk": "META"},
                UpdateExpression="SET created_at = if_not_exists(created_at, :c)",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":c": item["created_at"]},
            )
        except Exception:
            logger.exception("created_at was not saved")


# The name of a box is the first line of its description without Markdown, as
# the page shows it (plainTitle in web/app.js).
def plain_title(text):
    line = next((part.strip() for part in (text or "").split("\n") if part.strip()), "")
    line = re.sub(r"^#{1,6}\s+", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    return re.sub(r"[*_`]", "", line).strip()


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


# Search that also looks through the history, asked for with history=1. The
# filter cannot fold old texts, so cards and history entries are read and
# matched here. A box found only by its history is marked history_match. A box
# whose entries straddle two pages may come back on both; the page drops the
# repeat.
def search_with_history(tokens, cursor, limit):
    from boto3.dynamodb.conditions import Attr

    filt = Attr("sk").eq("META") | Attr("sk").begins_with("HIST#")
    found = {}
    by_history = set()
    start = cursor
    pages = 0
    last_key = None
    while pages < 25:
        kwargs = {"FilterExpression": filt, "Limit": 100}
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table().scan(**kwargs)
        pages += 1
        for item in resp.get("Items", []):
            sk = item.get("sk") or ""
            if sk == "META" and all(token in (item.get("search_text") or "") for token in tokens):
                found[item["pk"]] = item
            elif sk.startswith("HIST#") and history_matches(item, tokens):
                by_history.add(item["pk"])
        last_key = resp.get("LastEvaluatedKey")
        if len(found) + len(by_history - set(found)) >= limit or not last_key:
            break
        start = last_key
    for pk in by_history - set(found):
        meta = table().get_item(Key={"pk": pk, "sk": "META"}).get("Item")
        if meta:
            found[pk] = dict(meta, history_match=True)
    items = sorted(found.values(), key=lambda item: item.get("gsi1sk") or item.get("number") or "")
    return items, last_key


def history_matches(item, tokens):
    number = (item.get("pk") or "").removeprefix("BOX#")
    text = build_search(number, item.get("note") or "", item.get("contents_before") or "", item.get("contents_after") or "")
    return all(token in text for token in tokens)


def create_box(event):
    payload = body_json(event)
    number = validate_number(payload.get("number"))
    description = clean_text(payload.get("description"), 4000, "Описание")
    contents = clean_text(payload.get("contents"), 20000, "Содержимое")
    # The history and photos of a box in the trash stay under its number, so
    # the number is free again only once that box is restored or purged.
    if get_trashed(number):
        raise ApiError(409, "Коробка с таким номером лежит в корзине. Восстановите её или удалите навсегда в настройках.")
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
        "created_at": now,
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
    actions.append(draft_delete(who, DRAFT_NEW))
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
    actions.append(draft_delete(who, draft_sk(number)))
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


# Drafts -------------------------------------------------------------------
# What a user has typed in a card, or in the new box form, and not saved yet
# is kept for that user alone, under USER#<email> with SK=DRAFT#<number>, or
# DRAFT#NEW for the form, so that it outlives closing the card, a reload and
# the device. version is the version of the card the edit began from. Saving
# the card, or creating the box, deletes the draft in the same transaction.

DRAFT_NEW = "DRAFT#NEW"


def draft_sk(number):
    return DRAFT_NEW if number is None else "DRAFT#" + validate_number(number)


def draft_delete(who, sk):
    return {"Delete": {"TableName": table_name(), "Key": {"pk": marshal("USER#" + who), "sk": marshal(sk)}}}


def list_drafts(event):
    from boto3.dynamodb.conditions import Key

    items = []
    start = None
    while True:
        kwargs = {"KeyConditionExpression": Key("pk").eq(user_pk(event)) & Key("sk").begins_with("DRAFT#")}
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table().query(**kwargs)
        items.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            return response(200, {"items": [public_draft(item) for item in items]})


# The text is kept as typed, spaces at the ends too; the number of the new box
# form may not be a valid one yet.
def put_draft(number, event):
    sk = draft_sk(number)
    payload = body_json(event)
    item = {
        "pk": user_pk(event),
        "sk": sk,
        "number": clean_text(payload.get("number"), 41, "Номер", strip=False) if number is None else number,
        "description": clean_text(payload.get("description"), 4000, "Описание", strip=False),
        "contents": clean_text(payload.get("contents"), 20000, "Содержимое", strip=False),
        "note": clean_text(payload.get("note"), 500, "Комментарий", strip=False),
        "version": max(as_int(payload.get("version"), 0), 0),
        "saved_at": now_iso(),
    }
    table().put_item(Item=item)
    return response(200, public_draft(item))


def delete_draft(number, event):
    table().delete_item(Key={"pk": user_pk(event), "sk": draft_sk(number)})
    return response(200, {"deleted": True})


def public_draft(item):
    return {
        "new": item.get("sk") == DRAFT_NEW,
        "number": item.get("number") or "",
        "description": item.get("description") or "",
        "contents": item.get("contents") or "",
        "note": item.get("note") or "",
        "version": as_int(item.get("version"), 0),
        "saved_at": item.get("saved_at") or "",
    }


# A deleted box goes to the trash: its card moves from SK=META to SK=TRASH,
# out of the list, search and the box index, while its history and photos
# stay. The trash is listed from the "boxes" index under gsi1pk=TRASH, newest
# first; from there a box is restored or purged for good.
def delete_box(number, event):
    current = require_meta(number)
    now = now_iso()
    trashed = dict(current, sk="TRASH", gsi1pk="TRASH", gsi1sk=now, deleted_at=now, deleted_by=actor(event))
    transact([
        {"Put": {
            "TableName": table_name(),
            "Item": marshal_item(trashed),
            "ConditionExpression": "attribute_not_exists(pk)",
        }},
        {"Delete": {
            "TableName": table_name(),
            "Key": {"pk": marshal(box_pk(number)), "sk": marshal("META")},
            "ConditionExpression": "version = :v",
            "ExpressionAttributeValues": {":v": marshal(as_int(current.get("version"), 1))},
        }},
        draft_delete(actor(event), draft_sk(number)),
    ])
    return response(200, {"deleted": True, "trash": True})


def get_trashed(number):
    return table().get_item(Key={"pk": box_pk(number), "sk": "TRASH"}).get("Item")


def list_trash():
    from boto3.dynamodb.conditions import Key

    items = []
    start = None
    while True:
        kwargs = {"IndexName": "boxes", "KeyConditionExpression": Key("gsi1pk").eq("TRASH"), "ScanIndexForward": False}
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table().query(**kwargs)
        items.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
    return response(200, {
        "items": [dict(public_card(item), deleted_at=item.get("deleted_at") or "", deleted_by=item.get("deleted_by") or "") for item in items],
        "urls_expire_at": urls_expire_at(),
    })


def restore_box(number, event):
    number = validate_number(number)
    trashed = get_trashed(number)
    if not trashed:
        raise ApiError(404, "В корзине нет такой коробки")
    restored = {key: value for key, value in trashed.items() if key not in {"deleted_at", "deleted_by"}}
    restored.update({
        "sk": "META",
        "gsi1pk": "BOX",
        "gsi1sk": sort_key(number),
        "updated_at": now_iso(),
        "updated_by": actor(event),
        "version": as_int(trashed.get("version"), 1) + 1,
    })
    try:
        transact([
            {"Put": {
                "TableName": table_name(),
                "Item": marshal_item(restored),
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Delete": {
                "TableName": table_name(),
                "Key": {"pk": marshal(box_pk(number)), "sk": marshal("TRASH")},
                "ConditionExpression": "attribute_exists(pk)",
            }},
        ])
    except Exception as exc:
        if conditional_failed(exc):
            raise ApiError(409, "Коробка с таким номером уже есть") from exc
        raise
    return response(200, public_detail(restored))


# Purging removes the box with its history and photos; only administrators
# do it, as with history entries.
def purge_box(number, event):
    if not is_admin(event):
        raise ApiError(403, "Нужны права администратора")
    number = validate_number(number)
    items = query_all(number)
    if not any(item.get("sk") == "TRASH" for item in items):
        raise ApiError(404, "В корзине нет такой коробки")
    if any(item.get("sk") == "META" for item in items):
        raise ApiError(409, "Коробка с таким номером уже есть")
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
    items = []
    for item in resp.get("Items", []):
        entry = {
            "id": item["sk"].removeprefix("HIST#"),
            "at": item.get("changed_at"),
            "by": item.get("changed_by") or "",
            "field": item.get("field") or "contents",
            "before": item.get("contents_before") or "",
            "after": item.get("contents_after") or "",
            "note": item.get("note") or "",
        }
        if entry["field"] == "photo":
            for side in ("before", "after"):
                entry[side + "_photo_url"] = signed_get(item.get(f"photo_{side}") or "")
                entry[side + "_thumb_url"] = signed_get(item.get(f"thumb_{side}") or item.get(f"photo_{side}") or "")
        items.append(entry)
    return response(200, {"items": items})


HISTORY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]{8,16}Z#[0-9a-f]{8}$")


# History is the record of who changed what, so only administrators remove
# entries from it. The card itself stays as it is.
def delete_history_entry(number, entry_id, event):
    if not is_admin(event):
        raise ApiError(403, "Нужны права администратора")
    number = validate_number(number)
    if not HISTORY_ID_RE.fullmatch(entry_id or ""):
        raise ApiError(400, "Некорректная запись истории")
    try:
        old = table().delete_item(
            Key={"pk": box_pk(number), "sk": "HIST#" + entry_id},
            ConditionExpression="attribute_exists(pk)",
            ReturnValues="ALL_OLD",
        )
    except Exception as exc:
        if conditional_failed(exc):
            raise ApiError(404, "Такой записи в истории нет") from exc
        raise
    # An earlier photo that no other entry and not the card refers to goes too.
    if ((old or {}).get("Attributes") or {}).get("field") == "photo":
        cleanup_prefix(number, kept_photo_keys(number))
    return response(200, {"deleted": entry_id})


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
    # A turned photo shows the same things, so it keeps its description and is
    # not sent to recognition again.
    keep = bool(payload.get("keep_description"))
    hidden = (current.get("hidden_text") or "") if keep else ""
    pending = not keep and recognition_configured(who)
    values = {
        ":p": original_key,
        ":t": thumb_key,
        ":c": head.get("ContentType") or "image/jpeg",
        ":s": build_search(number, current.get("description") or "", current.get("contents") or "", hidden),
        ":h": hidden,
        ":u": now,
        ":b": who,
        ":nv": version + 1,
        ":ov": version,
    }
    expression = "SET photo_key = :p, thumb_key = :t, photo_content_type = :c, search_text = :s, hidden_text = :h, updated_at = :u, updated_by = :b, version = :nv"
    actions = [{
        "Update": {
            "TableName": table_name(),
            "Key": {"pk": marshal(box_pk(number)), "sk": marshal("META")},
            "UpdateExpression": expression,
            "ConditionExpression": "version = :ov",
            "ExpressionAttributeValues": {key: marshal(value) for key, value in values.items()},
        }
    }]
    if keep:
        # A turned photo takes the place of the old one: no history entry, and
        # the entries that showed the old photo show the turned one, so the old
        # files go with the cleanup below.
        actions.extend(turned_photo_updates(number, current, original_key, thumb_key))
    else:
        # The old photo stays in the bucket, and the history shows it next to
        # the new one; so does the description of the old photo, which goes
        # with it.
        photo = history_item(number, "", "", now, who, "", field="photo")
        photo.update({
            "photo_before": current.get("photo_key") or "",
            "thumb_before": current.get("thumb_key") or "",
            "photo_after": original_key,
            "thumb_after": thumb_key,
        })
        actions.append({"Put": {"TableName": table_name(), "Item": marshal_item(photo)}})
    old_hidden = current.get("hidden_text") or ""
    if old_hidden and not keep:
        actions.append({"Put": {"TableName": table_name(), "Item": marshal_item(history_item(
            number, old_hidden, "", now, who, "", field="hidden_text"
        ))}})
    try:
        transact(actions)
    except Exception as exc:
        if conditional_failed(exc):
            raise ApiError(409, "Коробку уже изменили. Повторите загрузку фотографии.") from exc
        raise
    # Uploads that were never completed go; photos the history shows stay.
    cleanup_prefix(number, {original_key, thumb_key} | kept_photo_keys(number))
    if pending:
        try:
            queue_recognition(number, who, thumb_key, request_language(event))
        except Exception as exc:
            logger.exception("queue recognition failed")
            RecognitionLog(who, number).finish("error", "Не удалось запустить распознавание", exception_text(exc))
            pending = False
    updated = dict(current)
    updated.update({
        "photo_key": original_key,
        "thumb_key": thumb_key,
        "photo_content_type": head.get("ContentType") or "image/jpeg",
        "hidden_text": hidden,
        "updated_at": now,
        "updated_by": who,
        "version": version + 1,
    })
    body = public_detail(updated)
    body["recognition_pending"] = pending
    return response(200, body)


def turned_photo_updates(number, current, original_key, thumb_key):
    old = current.get("photo_key") or ""
    actions = []
    for item in query_all(number):
        if not item.get("sk", "").startswith("HIST#"):
            continue
        sides = [side for side in ("before", "after") if old and item.get("photo_" + side) == old]
        if not sides:
            continue
        actions.append({"Update": {
            "TableName": table_name(),
            "Key": {"pk": marshal(item["pk"]), "sk": marshal(item["sk"])},
            "UpdateExpression": "SET " + ", ".join(f"photo_{side} = :p, thumb_{side} = :t" for side in sides),
            "ExpressionAttributeValues": {":p": marshal(original_key), ":t": marshal(thumb_key)},
        }})
    return actions


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


def finish_recognition(number, user_email, thumb_key, lang="ru", note=""):
    if not recognition_enabled():
        return
    number = validate_number(number)
    log = RecognitionLog(user_email, number)
    current = get_meta(number)
    if not current:
        log.finish("skipped", "Коробку удалили до распознавания")
        return
    if current.get("thumb_key") != thumb_key:
        log.finish("skipped", "Фотографию заменили до распознавания")
        return
    try:
        hidden = describe_photo(user_email, thumb_key, lang, log)
    except RecognitionSkipped:
        log.finish("skipped", "Ключ или модель Grok не заданы")
        return
    except ApiError as exc:
        log.finish("error", exc.message, exc.detail)
        return
    except Exception as exc:
        log.finish("error", "Внутренняя ошибка", exception_text(exc))
        raise
    before = current.get("hidden_text") or ""
    actions = [{
        "Update": {
            "TableName": table_name(),
            "Key": {"pk": marshal(box_pk(number)), "sk": marshal("META")},
            "UpdateExpression": "SET hidden_text = :h, search_text = :s",
            "ConditionExpression": "thumb_key = :t",
            "ExpressionAttributeValues": {
                ":h": marshal(hidden),
                ":s": marshal(build_search(number, current.get("description") or "", current.get("contents") or "", hidden)),
                ":t": marshal(thumb_key),
            },
        }
    }]
    # Recognition signs the entry with the name of whoever uploaded the photo:
    # their key paid for it.
    if hidden != before:
        actions.append({"Put": {"TableName": table_name(), "Item": marshal_item(history_item(
            number, before, hidden, now_iso(), user_email, note, field="hidden_text"
        ))}})
    try:
        transact(actions)
    except Exception as exc:
        if conditional_failed(exc):
            log.finish("skipped", "Фотографию заменили во время распознавания")
            return
        log.finish("error", "Внутренняя ошибка", exception_text(exc))
        raise
    log.finish("ok", chars=len(hidden))


# Recognition of every photo again, with the user's current model, prompt and
# the language chosen for it. The function calls itself in the background and
# goes through the boxes one by one, so Grok gets one photo at a time; when
# less than a minute of its time is left, it hands the rest of the list to a
# new call. The run is kept per user under SK=RERUN: a new run replaces it,
# and a stopped one ends before the next box.
RERUN_MARGIN_MS = 60_000


def start_rerun(event):
    who = actor(event)
    if not recognition_enabled():
        raise ApiError(403, "Распознавание фотографий выключено")
    if not recognition_configured(who):
        raise ApiError(400, "Сначала сохраните ключ и модель Grok")
    payload = body_json(event)
    lang = payload.get("lang") if payload.get("lang") in LANGUAGES else request_language(event)
    numbers = []
    start = None
    while True:
        page, start = query_boxes(start, 1000)
        numbers += [item["number"] for item in page if item.get("thumb_key")]
        if not start:
            break
    run = {
        "pk": f"USER#{who}",
        "sk": "RERUN",
        "run": uuid.uuid4().hex,
        "status": "running" if numbers else "done",
        "lang": lang,
        "total": len(numbers),
        "done": 0,
        "started_at": now_iso(),
    }
    table().put_item(Item=run)
    if numbers:
        queue_rerun(who, run["run"], numbers, lang)
    return response(200, {"rerun": rerun_status(run)})


def stop_rerun(event):
    key = {"pk": user_pk(event), "sk": "RERUN"}
    run = table().get_item(Key=key).get("Item")
    if run and run.get("status") == "running":
        run.update(status="stopped", finished_at=now_iso())
        table().put_item(Item=run)
    return response(200, {"rerun": rerun_status(run)})


def rerun_status(run):
    if not run:
        return None
    return {key: run.get(key) for key in ("status", "lang", "started_at", "finished_at")} | {
        "total": as_int(run.get("total")),
        "done": as_int(run.get("done")),
    }


def queue_rerun(user_email, run_id, numbers, lang):
    import boto3

    boto3.client("lambda").invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"task": "recognize_all", "user": user_email, "run": run_id, "numbers": numbers, "lang": lang}).encode("utf-8"),
    )


def recognize_all(user_email, run_id, numbers, lang, context=None):
    key = {"pk": f"USER#{user_email}", "sk": "RERUN"}
    for index, number in enumerate(numbers):
        run = table().get_item(Key=key).get("Item") or {}
        if run.get("run") != run_id or run.get("status") != "running":
            return
        if context and context.get_remaining_time_in_millis() < RERUN_MARGIN_MS:
            queue_rerun(user_email, run_id, numbers[index:], lang)
            return
        meta = get_meta(number)
        if meta and meta.get("thumb_key"):
            try:
                finish_recognition(number, user_email, meta["thumb_key"], lang, note="Повторное распознавание")
            except Exception:
                logger.exception("recognition of %s failed", number)
        run["done"] = as_int(run.get("done")) + 1
        table().put_item(Item=run)
    run = table().get_item(Key=key).get("Item") or {}
    if run.get("run") == run_id and run.get("status") == "running":
        run.update(status="done", finished_at=now_iso())
        table().put_item(Item=run)


# Recognition runs in the background, so its outcome is kept per user under
# USER#<email>, SK=RECOG#..., and shown in the settings. An attempt is written
# as running first: if the function is stopped half-way, the entry stays so.
# DynamoDB drops entries after RECOGNITION_LOG_DAYS through expires_at (TTL).
# The entries also sit in the "boxes" index under gsi1pk=RECOG, newest last, so
# administrators read everyone's log with one query; the box list asks for BOX.
RECOGNITION_LOG_DAYS = 30
RECOGNITION_LOST_AFTER = timedelta(minutes=3)


class RecognitionLog:
    def __init__(self, user_email, number):
        self.started = datetime.now(timezone.utc)
        started = now_iso()
        self.item = {
            "pk": f"USER#{user_email}",
            "sk": f"RECOG#{started}#{uuid.uuid4().hex[:8]}",
            "gsi1pk": "RECOG",
            "gsi1sk": started,
            "user": user_email,
            "number": number,
            "status": "running",
            "started_at": started,
            "expires_at": int((self.started + timedelta(days=RECOGNITION_LOG_DAYS)).timestamp()),
        }
        self.write()

    def finish(self, status, message="", detail="", chars=0):
        self.item.update({
            "status": status,
            "message": message,
            "detail": (detail or "")[:2000],
            "chars": chars,
            "duration_ms": int((datetime.now(timezone.utc) - self.started).total_seconds() * 1000),
        })
        self.write()

    # The log must never break recognition itself.
    def write(self):
        try:
            table().put_item(Item=self.item)
        except Exception:
            logger.exception("recognition log was not written")


def get_recognition_log(event):
    from boto3.dynamodb.conditions import Key

    resp = table().query(
        KeyConditionExpression=Key("pk").eq(user_pk(event)) & Key("sk").begins_with("RECOG#"),
        ScanIndexForward=False,
        Limit=50,
    )
    return response(200, {"items": log_entries(resp.get("Items", []), request_language(event))})


def admin_recognition_log(event):
    from boto3.dynamodb.conditions import Key

    resp = table().query(
        IndexName="boxes",
        KeyConditionExpression=Key("gsi1pk").eq("RECOG"),
        ScanIndexForward=False,
        Limit=100,
    )
    return response(200, {"items": log_entries(resp.get("Items", []), request_language(event))})


def log_entries(rows, lang):
    lost_before = (datetime.now(timezone.utc) - RECOGNITION_LOST_AFTER).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    items = []
    for item in rows:
        status = item.get("status") or ""
        message = item.get("message") or ""
        if status == "running" and (item.get("started_at") or "") < lost_before:
            status = "lost"
            message = "Распознавание не завершилось: функцию остановили по времени или из-за сбоя"
        items.append({
            "at": item.get("started_at") or "",
            "user": item.get("user") or (item.get("pk") or "").removeprefix("USER#"),
            "number": item.get("number") or "",
            "status": status,
            "message": translate(lang, message) if message else "",
            "detail": item.get("detail") or "",
            "model": item.get("model") or "",
            "duration_ms": as_int(item.get("duration_ms")),
            "chars": as_int(item.get("chars")),
        })
    return items


def exception_text(exc):
    return f"{type(exc).__name__}: {exc}"[:2000]


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


# field says what changed: the contents, or hidden_text, the photo description
# written by recognition. Entries from before the field existed are contents.
def history_item(number, before, after, now, who, note, field="contents"):
    item = {
        "pk": box_pk(number),
        "sk": f"HIST#{now}#{uuid.uuid4().hex[:8]}",
        "contents_before": before,
        "contents_after": after,
        "changed_at": now,
        "changed_by": who,
        "note": note,
    }
    if field != "contents":
        item["field"] = field
    return item


# The photo of the card, in the trash too, and every photo its history shows.
def kept_photo_keys(number):
    keys = set()
    for item in query_all(number):
        names = ("photo_key", "thumb_key") if item.get("sk") in {"META", "TRASH"} else ("photo_before", "thumb_before", "photo_after", "thumb_after")
        keys.update(item.get(name) for name in names if item.get(name))
    return keys


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
        "created_at": item.get("created_at") or "",
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


def clean_text(value, limit, field, strip=True):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "{field} должно быть текстом", field=field)
    value = value.replace("\r\n", "\n")
    if strip:
        value = value.strip()
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


# A prompt of the user's own says what to write; the language is added to it.
ANSWER_LANGUAGE = {
    "ru": "Пиши по-русски.",
    "en": "Write in English.",
    "fr": "Écris en français.",
    "it": "Scrivi in italiano.",
}


def recognition_prompt(settings, lang):
    own = (settings.get("grok_prompt") or "").strip()
    if not own:
        return RECOGNITION_PROMPTS.get(lang, RECOGNITION_PROMPTS["ru"])
    return own + "\n\n" + ANSWER_LANGUAGE.get(lang, ANSWER_LANGUAGE["ru"])


class RecognitionSkipped(Exception):
    pass


# The Grok key is write-only: the page learns that a key is saved and its last
# four characters, never the key itself, so it cannot leak through the browser.
def get_settings(event):
    if not recognition_enabled():
        actor(event)
        return response(200, dict(key_status({}), recognition=False, grok_model=""))
    item = table().get_item(Key={"pk": user_pk(event), "sk": "SETTINGS"}).get("Item") or {}
    run = table().get_item(Key={"pk": user_pk(event), "sk": "RERUN"}).get("Item")
    return response(200, dict(
        key_status(item),
        recognition=True,
        default_prompt=RECOGNITION_PROMPTS[request_language(event)],
        rerun=rerun_status(run),
    ))


# An empty key keeps the saved one; remove_key deletes it.
def put_settings(event):
    if not recognition_enabled():
        raise ApiError(403, "Распознавание фотографий выключено")
    payload = body_json(event)
    api_key = clean_text(payload.get("grok_api_key"), 500, "API-ключ")
    model = clean_text(payload.get("grok_model"), 80, "Модель") or "grok-4.7"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", model):
        raise ApiError(400, "Некорректное название модели")
    key = {"pk": user_pk(event), "sk": "SETTINGS"}
    saved = table().get_item(Key=key).get("Item") or {}
    # An empty prompt is the built-in one; a missing field keeps the saved one.
    prompt = clean_text(payload.get("grok_prompt"), 4000, "Промпт") if "grok_prompt" in payload else saved.get("grok_prompt") or ""
    if payload.get("remove_key"):
        api_key = ""
    elif not api_key:
        api_key = saved.get("grok_api_key") or ""
    item = dict(key, grok_api_key=api_key, grok_model=model, grok_prompt=prompt, updated_at=now_iso())
    table().put_item(Item=item)
    return response(200, dict(key_status(item), saved=True))


def key_status(item):
    api_key = (item.get("grok_api_key") or "").strip()
    return {
        "grok_key_set": bool(api_key),
        "grok_key_hint": api_key[-4:] if len(api_key) >= 12 else "",
        "grok_model": item.get("grok_model") or "grok-4.7",
        "grok_prompt": item.get("grok_prompt") or "",
    }


def user_pk(event):
    return "USER#" + actor(event)


# Users --------------------------------------------------------------------
# Members of the Cognito admin group invite, disable and delete users and
# reset two-factor authentication for someone who lost their phone.

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
    if parts == ["recognition-log"] and method == "GET":
        return admin_recognition_log(event)
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


def describe_photo(user_email, thumb_key, lang="ru", log=None):
    settings = table().get_item(Key={"pk": f"USER#{user_email}", "sk": "SETTINGS"}).get("Item") or {}
    api_key = (settings.get("grok_api_key") or "").strip()
    model = (settings.get("grok_model") or "").strip()
    if not api_key or not model:
        raise RecognitionSkipped()
    if log:
        log.item["model"] = model
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
                    "text": recognition_prompt(settings, lang),
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
    # What Grok answered goes to the recognition log; the key is cut out of it.
    def scrub(text):
        return text.replace(api_key, "***")[:2000]

    try:
        with urllib.request.urlopen(request, timeout=50) as reply:
            raw_reply = reply.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        logger.warning("grok status %s", exc.code)
        detail = scrub(f"HTTP {exc.code}: {read_error_body(exc)}")
        if exc.code in {401, 403}:
            raise ApiError(502, "Проверьте API-ключ Grok в настройках", detail=detail) from exc
        raise ApiError(502, "Сервис распознавания не принял фотографию", detail=detail) from exc
    except urllib.error.URLError as exc:
        logger.warning("grok network error")
        raise ApiError(502, "Сервис распознавания недоступен", detail=scrub(str(exc.reason))) from exc
    except TimeoutError as exc:
        logger.warning("grok timeout")
        raise ApiError(502, "Сервис распознавания не ответил вовремя", detail="no answer in 50 s") from exc
    try:
        data = json.loads(raw_reply)
    except ValueError as exc:
        raise ApiError(502, "Сервис распознавания вернул непонятный ответ", detail=scrub(raw_reply)) from exc
    text = extract_response_text(data).strip()
    if not text:
        output_types = [item.get("type") for item in data.get("output") or [] if isinstance(item, dict)]
        detail = f"status={data.get('status')}, incomplete_details={data.get('incomplete_details')}, output={output_types}"
        raise ApiError(502, "Сервис распознавания не вернул описание", detail=scrub(detail))
    return text[:4000]


def read_error_body(exc):
    try:
        return exc.read(2000).decode("utf-8", errors="replace")
    except Exception:
        return ""


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
    "Номер": ("Number", "Numéro", "Numero"),
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
    "Сервис распознавания не ответил вовремя": (
        "The recognition service did not answer in time",
        "Le service de reconnaissance n’a pas répondu à temps",
        "Il servizio di riconoscimento non ha risposto in tempo",
    ),
    "Сервис распознавания вернул непонятный ответ": (
        "The recognition service returned an unreadable answer",
        "Le service de reconnaissance a renvoyé une réponse illisible",
        "Il servizio di riconoscimento ha restituito una risposta illeggibile",
    ),
    "Не удалось запустить распознавание": ("Recognition could not be started", "La reconnaissance n’a pas pu démarrer", "Impossibile avviare il riconoscimento"),
    "Коробку удалили до распознавания": ("The box was deleted before recognition", "Le carton a été supprimé avant la reconnaissance", "La scatola è stata eliminata prima del riconoscimento"),
    "Фотографию заменили до распознавания": ("The photo was replaced before recognition", "La photo a été remplacée avant la reconnaissance", "La foto è stata sostituita prima del riconoscimento"),
    "Фотографию заменили во время распознавания": ("The photo was replaced during recognition", "La photo a été remplacée pendant la reconnaissance", "La foto è stata sostituita durante il riconoscimento"),
    "Ключ или модель Grok не заданы": ("The Grok key or model is not set", "La clé ou le modèle Grok n’est pas défini", "La chiave o il modello Grok non sono impostati"),
    "Распознавание не завершилось: функцию остановили по времени или из-за сбоя": (
        "Recognition did not finish: the function was stopped by its time limit or a failure",
        "La reconnaissance ne s’est pas terminée : la fonction a été arrêtée par sa limite de temps ou une panne",
        "Il riconoscimento non è terminato: la funzione è stata fermata dal limite di tempo o da un guasto",
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
    "Некорректная запись истории": ("Invalid history entry", "Entrée d’historique invalide", "Voce della cronologia non valida"),
    "Такой записи в истории нет": ("There is no such history entry", "Cette entrée d’historique n’existe pas", "Questa voce della cronologia non esiste"),
    "Нужен адрес почты": ("An email address is needed", "Une adresse e-mail est requise", "Serve un indirizzo email"),
    "Коробка с таким номером лежит в корзине. Восстановите её или удалите навсегда в настройках.": (
        "A box with this number is in the trash. Restore it or delete it for good in the settings.",
        "Un carton portant ce numéro est dans la corbeille. Restaurez-le ou supprimez-le définitivement dans les paramètres.",
        "Una scatola con questo numero è nel cestino. Ripristinala o eliminala definitivamente nelle impostazioni.",
    ),
    "В корзине нет такой коробки": ("There is no such box in the trash", "Ce carton n’est pas dans la corbeille", "Questa scatola non è nel cestino"),
    "Сначала сохраните ключ и модель Grok": ("Save the Grok key and model first", "Enregistrez d’abord la clé et le modèle Grok", "Salva prima la chiave e il modello Grok"),
    "Промпт": ("Prompt", "Prompt", "Prompt"),
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


# Versions -----------------------------------------------------------------

# The version of this deployment (VERSION) and the releases of the demo
# repository (RELEASES_REPOSITORY, owner/name) that came after it. They come
# from the Atom feed of the repository's GitHub releases, a public page that
# the hourly limit of the REST API, shared by every function behind Lambda's
# addresses, does not touch. The list stays an hour in the warm function, and
# the last one read serves while GitHub does not answer.
RELEASES_SECONDS = 3600
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ATOM = "{http://www.w3.org/2005/Atom}"
_releases = None


def version_info():
    current = os.environ.get("VERSION", "")
    repository = os.environ.get("RELEASES_REPOSITORY", "")
    info = {
        "version": current,
        "repository": repository,
        "url": f"https://github.com/{repository}/releases" if repository else None,
        "latest": None,
        "newer": [],
        "checked_at": None,
        "error": None,
    }
    if not repository:
        return info
    try:
        releases, checked_at = github_releases(repository)
    except Exception:
        logger.exception("releases of %s not read", repository)
        info["error"] = "unavailable"
        return info
    info["checked_at"] = checked_at
    info["latest"] = releases[0]["version"] if releases else None
    info["newer"] = [release for release in releases if version_key(release["version"]) > version_key(current)]
    return info


def version_key(value):
    match = VERSION_RE.match(value or "")
    return tuple(int(part) for part in match.groups()) if match else ()


def github_releases(repository):
    global _releases
    now = time.time()
    if _releases and _releases[0] == repository and now - _releases[1] < RELEASES_SECONDS:
        return _releases[2], _releases[3]
    try:
        request = urllib.request.Request(
            f"https://github.com/{repository}/releases.atom",
            headers={"Accept": "application/atom+xml", "User-Agent": "release-check (+https://github.com/" + repository + ")"},
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            releases = parse_releases(answer.read(2_000_000))
    except Exception:
        if _releases and _releases[0] == repository:
            logger.warning("releases of %s not read; the list of %s serves", repository, _releases[3], exc_info=True)
            return _releases[2], _releases[3]
        raise
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _releases = (repository, now, releases, checked_at)
    return releases, checked_at


# The entries of the feed whose tag is a version, newest version first, with
# their notes as plain text.
def parse_releases(feed):
    import xml.etree.ElementTree as ElementTree

    releases = []
    for entry in ElementTree.fromstring(feed).findall(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        url = link.get("href", "") if link is not None else ""
        version = unquote(url.rstrip("/").rsplit("/", 1)[-1])
        if not version_key(version):
            continue
        releases.append({
            "version": version,
            "title": (entry.findtext(ATOM + "title") or version).strip(),
            "date": (entry.findtext(ATOM + "updated") or "")[:10],
            "url": url,
            "notes": release_notes(entry.findtext(ATOM + "content") or "")[:4000],
        })
    releases.sort(key=lambda release: version_key(release["version"]), reverse=True)
    return releases


class ReleaseNotes(html.parser.HTMLParser):
    BREAKS = {"p", "div", "br", "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def release_notes(value):
    parser = ReleaseNotes()
    parser.feed(value)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
