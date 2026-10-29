import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lambda_app


class InventoryHelpersTest(unittest.TestCase):
    def test_fold_treats_yo_as_ye_and_collapses_space(self):
        self.assertEqual(lambda_app.fold("  Ёлка\nзеленая  "), "елка зеленая")

    def test_query_tokens_keep_number_and_russian_word(self):
        self.assertEqual(lambda_app.query_tokens("Коробка 12, провода"), ["коробка", "12", "провода"])

    def test_search_text_contains_folded_contents(self):
        text = lambda_app.build_search("12", "Ёлка", "Гирлянда и провод")
        self.assertIn("елка", text)
        self.assertIn("гирлянда", text)
        self.assertIn("12", text)

    def test_hidden_photo_text_is_searchable_and_folded(self):
        text = lambda_app.build_search("2", "", "наждачка", "Жёлтая Ёмкость")
        self.assertIn("наждачка", text)
        self.assertIn("емкость", text)
        self.assertNotIn("ё", text)

    def test_media_path_is_stable(self):
        self.assertEqual(lambda_app.signed_get("boxes/1/original/abc.jpg"), "/media/boxes/1/original/abc.jpg")
        self.assertIsNone(lambda_app.signed_get(""))

    def test_extract_response_text_reads_output_blocks(self):
        text = lambda_app.extract_response_text({
            "output": [{"content": [{"type": "output_text", "text": "рубанок и стамеска"}]}],
        })
        self.assertEqual(text, "рубанок и стамеска")

    def test_sort_key_orders_numbers_numerically(self):
        self.assertLess(lambda_app.sort_key("2"), lambda_app.sort_key("10"))
        self.assertLess(lambda_app.sort_key("10"), lambda_app.sort_key("A-1"))

    def test_number_validation(self):
        self.assertEqual(lambda_app.validate_number("A-12"), "A-12")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.validate_number("../12")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.validate_number("")

    def test_photo_key_must_stay_inside_the_box(self):
        good = "boxes/12/original/" + ("a" * 32) + ".jpg"
        self.assertTrue(lambda_app.key_allowed("12", good, "original"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/12/original/../99/" + ("a" * 32) + ".jpg", "original"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/99/original/" + ("a" * 32) + ".jpg", "original"))
        thumb = "boxes/12/thumb/" + ("b" * 32) + ".jpg"
        self.assertTrue(lambda_app.key_allowed("12", thumb, "thumb"))
        self.assertFalse(lambda_app.key_allowed("12", "boxes/12/thumb/" + ("b" * 32) + ".png", "thumb"))

    def test_cursor_roundtrip(self):
        key = {"pk": "BOX#12", "sk": "META"}
        self.assertEqual(lambda_app.decode_cursor(lambda_app.encode_cursor(key)), key)
        self.assertIsNone(lambda_app.decode_cursor(None))
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.decode_cursor("%%%")

    def test_preview_flattens_and_truncates(self):
        self.assertEqual(lambda_app.preview("кабель\nи лампа"), "кабель и лампа")
        self.assertTrue(lambda_app.preview("а" * 200).endswith("…"))

    def test_clean_text_limits(self):
        self.assertEqual(lambda_app.clean_text(None, 10, "Описание"), "")
        with self.assertRaises(lambda_app.ApiError):
            lambda_app.clean_text("слишком длинно", 5, "Описание")


def openssl_key(tmp, command, *args):
    key_path = os.path.join(tmp, "key.pem")
    subprocess.run(["openssl", command, "-out", key_path, *args], check=True, capture_output=True)
    return key_path


def claims_event(claims):
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


class ActorTest(unittest.TestCase):
    def test_verified_email_names_the_user(self):
        event = claims_event({"email": "Person@Example.com", "email_verified": "true"})
        self.assertEqual(lambda_app.actor(event), "person@example.com")

    def test_unverified_or_missing_email_is_refused(self):
        for claims in ({"email": "person@example.com", "email_verified": "false"}, {"email": "person@example.com"}, {"sub": "abc"}):
            with self.assertRaises(lambda_app.ApiError) as caught:
                lambda_app.actor(claims_event(claims))
            self.assertEqual(caught.exception.status, 403)

    def test_settings_of_an_unverified_email_are_not_read(self):
        event = dict(claims_event({"email": "owner@example.com", "email_verified": "false"}), rawPath="/api/settings")
        event["requestContext"]["http"] = {"method": "GET"}

        class NoRead:
            def get_item(self, **kwargs):
                raise AssertionError("settings were read")

        lambda_app._table = NoRead()
        try:
            self.assertEqual(lambda_app.handler(event, None)["statusCode"], 403)
        finally:
            lambda_app._table = None


class LanguageTest(unittest.TestCase):
    def test_errors_follow_the_page_language(self):
        event = {"rawPath": "/api/nowhere", "headers": {"X-Inventory-Lang": "fr"}, "requestContext": {"http": {"method": "GET"}}}
        self.assertEqual(lambda_app.json.loads(lambda_app.handler(event, None)["body"])["error"], "Introuvable")

    def test_accept_language_and_russian_default(self):
        self.assertEqual(lambda_app.request_language({"headers": {"accept-language": "it-IT,it;q=0.9"}}), "it")
        self.assertEqual(lambda_app.request_language({"headers": {"accept-language": "de-DE"}}), "ru")

    def test_field_names_are_translated_too(self):
        with self.assertRaises(lambda_app.ApiError) as caught:
            lambda_app.clean_text("x" * 6, 5, "Описание")
        error = caught.exception
        self.assertEqual(lambda_app.translate("en", error.message, error.values), "Description is longer than 5 characters")
        self.assertEqual(lambda_app.translate("ru", error.message, error.values), "Описание длиннее 5 символов")

    def test_every_message_has_three_translations(self):
        self.assertTrue(all(len(value) == 3 for value in lambda_app.MESSAGES.values()))

    def test_query_tokens_keep_accented_words(self):
        self.assertEqual(lambda_app.query_tokens("Étagère, scatola 12"), ["étagère", "scatola", "12"])

    def test_recognition_prompt_per_language(self):
        self.assertEqual(set(lambda_app.RECOGNITION_PROMPTS), set(lambda_app.LANGUAGES))


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Pager:
    def __init__(self, fn):
        self.fn = fn

    def paginate(self, **kwargs):
        return [self.fn(**kwargs)]


class FakeCognito:
    def __init__(self):
        self.users = {}
        self.created = {}
        self.groups = {"admins": set()}

    def get_paginator(self, name):
        return Pager(getattr(self, name))

    def need(self, username):
        if username not in self.users:
            raise ClientError("UserNotFoundException")
        return self.users[username]

    def list_users(self, UserPoolId):
        return {"Users": [dict(user, Attributes=[{"Name": "email", "Value": email}]) for email, user in self.users.items()]}

    def list_users_in_group(self, UserPoolId, GroupName):
        return {"Users": [{"Username": email, "Attributes": [{"Name": "email", "Value": email}]} for email in self.groups[GroupName]]}

    def admin_get_user(self, UserPoolId, Username):
        return {"Username": Username, "UserMFASettingList": ["SOFTWARE_TOKEN_MFA"] if self.need(Username).get("mfa") else []}

    def admin_create_user(self, UserPoolId, Username, **kwargs):
        if Username in self.users:
            raise ClientError("UsernameExistsException")
        self.users[Username] = {"Username": Username, "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True,
                                "UserCreateDate": datetime.now(timezone.utc), "password": kwargs["TemporaryPassword"]}
        self.created[Username] = kwargs

    def admin_delete_user(self, UserPoolId, Username):
        self.need(Username)
        self.users.pop(Username)

    def admin_disable_user(self, UserPoolId, Username):
        self.need(Username)["Enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self.need(Username)["Enabled"] = True

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].add(Username)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].discard(Username)

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.need(Username).update(password=Password, UserStatus="FORCE_CHANGE_PASSWORD")

    def admin_set_user_mfa_preference(self, UserPoolId, Username, SoftwareTokenMfaSettings):
        self.need(Username)["mfa"] = SoftwareTokenMfaSettings["Enabled"]


class FakeTable:
    def __init__(self):
        self.items = {}

    def query(self, KeyConditionExpression, **kwargs):
        pk = KeyConditionExpression.get_expression()["values"][1]
        return {"Items": [{"pk": key[0], "sk": key[1]} for key in self.items if key[0] == pk]}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)


def call(method, path, body=None, params=None, email="person@example.com", groups=None):
    claims = {"email": email, "email_verified": "true"}
    if groups is not None:
        claims["cognito:groups"] = groups
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
    }
    result = lambda_app.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


class AdminTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", USER_POOL_ID="pool", ADMIN_GROUP="admins")
        self.cognito = lambda_app._cognito = FakeCognito()
        self.table = lambda_app._table = FakeTable()
        self.cognito.users["boss@example.com"] = {"Username": "boss@example.com", "UserStatus": "CONFIRMED", "Enabled": True,
                                                  "UserCreateDate": datetime.now(timezone.utc)}
        self.cognito.groups["admins"].add("boss@example.com")

    def tearDown(self):
        lambda_app._cognito = None
        lambda_app._table = None

    def admin(self, method, path, body=None, params=None):
        return call(method, path, body, params, email="boss@example.com", groups="[admins]")

    def users(self):
        return {user["email"]: user for user in self.admin("GET", "/api/admin/users")[1]["items"]}

    def test_only_admins_reach_admin_routes(self):
        self.assertEqual(call("GET", "/api/admin/users")[0], 403)
        self.assertEqual(call("GET", "/api/admin/users", groups="[readers]")[0], 403)
        self.assertEqual(self.admin("GET", "/api/admin/users")[0], 200)
        self.assertTrue(call("GET", "/api/me", email="boss@example.com", groups=["admins"])[1]["admin"])
        self.assertFalse(call("GET", "/api/me")[1]["admin"])

    def test_invite_update_and_delete_user(self):
        status, created = self.admin("POST", "/api/admin/users", {"email": " New@Example.com ", "admin": True})
        self.assertEqual(status, 200)
        self.assertEqual(created["email"], "new@example.com")
        password = created["temporary_password"]
        self.assertTrue(len(password) >= 12 and any(c.isupper() for c in password) and any(c in "!@#%&*-_=+" for c in password))
        self.assertEqual(self.admin("POST", "/api/admin/users", {"email": "new@example.com"})[0], 409)
        self.assertEqual(self.admin("POST", "/api/admin/users", {"email": "not-an-email"})[0], 400)
        self.assertTrue(self.users()["new@example.com"]["admin"])
        self.assertTrue(self.users()["boss@example.com"]["self"])

        self.admin("PUT", "/api/admin/users", {"email": "new@example.com", "enabled": False, "admin": False})
        self.assertFalse(self.users()["new@example.com"]["enabled"])
        self.assertFalse(self.users()["new@example.com"]["admin"])
        status, reset = self.admin("PUT", "/api/admin/users", {"email": "new@example.com", "reset_password": True})
        self.assertEqual(self.cognito.users["new@example.com"]["password"], reset["temporary_password"])

        self.table.items[("USER#new@example.com", "SETTINGS")] = {}
        self.table.items[("BOX#1", "META")] = {}
        self.assertEqual(self.admin("DELETE", "/api/admin/users", params={"email": "new@example.com"})[0], 200)
        self.assertNotIn("new@example.com", self.cognito.users)
        self.assertEqual(list(self.table.items), [("BOX#1", "META")], "settings go, boxes stay")

    def test_mfa_status_and_reset(self):
        self.cognito.users["phone@example.com"] = {"Username": "phone@example.com", "UserStatus": "CONFIRMED", "Enabled": True,
                                                   "UserCreateDate": datetime.now(timezone.utc), "mfa": True}
        self.assertTrue(self.users()["phone@example.com"]["mfa"])
        self.assertFalse(self.users()["boss@example.com"]["mfa"])
        self.assertEqual(call("PUT", "/api/admin/users", {"email": "phone@example.com", "reset_mfa": True})[0], 403)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "phone@example.com", "reset_mfa": True})[0], 200)
        self.assertFalse(self.cognito.users["phone@example.com"]["mfa"])

    def test_unknown_user_is_404(self):
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "nobody@example.com", "enabled": False})[0], 404)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "nobody@example.com", "reset_mfa": True})[0], 404)

    def test_demo_invite_sends_no_mail(self):
        os.environ["INVITE_EMAILS"] = "false"
        try:
            status, created = self.admin("POST", "/api/admin/users", {"email": "guest@example.com"})
        finally:
            os.environ["INVITE_EMAILS"] = "true"
        self.assertEqual(status, 200)
        self.assertFalse(created["emailed"])
        self.assertEqual(self.cognito.users["guest@example.com"]["password"], created["temporary_password"])
        self.assertEqual(self.cognito.created["guest@example.com"].get("MessageAction"), "SUPPRESS")

    def test_invite_emails_when_turned_on(self):
        os.environ["INVITE_EMAILS"] = "true"
        status, created = self.admin("POST", "/api/admin/users", {"email": "mail@example.com"})
        self.assertTrue(created["emailed"])
        self.assertEqual(self.cognito.created["mail@example.com"].get("DesiredDeliveryMediums"), ["EMAIL"])

    def test_admin_cannot_lock_themselves_out(self):
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "boss@example.com", "enabled": False})[0], 400)
        self.assertEqual(self.admin("PUT", "/api/admin/users", {"email": "boss@example.com", "admin": False})[0], 400)
        self.assertEqual(self.admin("DELETE", "/api/admin/users", params={"email": "boss@example.com"})[0], 400)


class RecognitionSwitchTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", RECOGNITION="false")

        class NoRead:
            def get_item(self, **kwargs):
                raise AssertionError("settings were read")

        lambda_app._table = NoRead()

    def tearDown(self):
        os.environ["RECOGNITION"] = "true"
        lambda_app._table = None

    def test_settings_do_not_hand_out_keys_when_off(self):
        status, body = call("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"recognition": False, "grok_key_set": False, "grok_key_hint": "", "grok_model": "", "grok_prompt": ""})

    def test_a_key_cannot_be_saved_when_off(self):
        self.assertEqual(call("PUT", "/api/settings", {"grok_api_key": "xai-secret", "grok_model": "grok-4.7"})[0], 403)

    def test_uploads_do_not_queue_recognition_when_off(self):
        self.assertFalse(lambda_app.recognition_configured("person@example.com"))


class FakeS3:
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(b"jpeg")}


class RecognitionLogTest(unittest.TestCase):
    KEY = "xai-secret-key-0123456789"
    THUMB = "boxes/5/thumb/" + "b" * 32 + ".jpg"

    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media")
        self.table = lambda_app._table = ScanTable([
            dict(meta("5", "Кабели"), thumb_key=self.THUMB),
            {"pk": "USER#owner@example.com", "sk": "SETTINGS", "grok_api_key": self.KEY, "grok_model": "grok-4.7"},
        ])
        lambda_app._s3 = FakeS3()
        self.saved = (lambda_app.transact, urllib.request.urlopen)
        lambda_app.transact = lambda actions: None

    def tearDown(self):
        lambda_app.transact, urllib.request.urlopen = self.saved
        lambda_app._table = None
        lambda_app._s3 = None

    def answer(self, body):
        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        urllib.request.urlopen = lambda request, timeout: Reply(json.dumps(body).encode())

    def fail(self, code, body):
        def urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO(body.encode()))

        urllib.request.urlopen = urlopen

    def log(self):
        status, body = call("GET", "/api/settings/recognition-log", email="owner@example.com")
        self.assertEqual(status, 200)
        return body["items"]

    def test_a_success_is_logged(self):
        self.answer({"output": [{"type": "message", "content": [{"type": "output_text", "text": "синие провода"}]}]})
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        [entry] = self.log()
        self.assertEqual((entry["status"], entry["number"], entry["model"], entry["chars"]), ("ok", "5", "grok-4.7", 13))

    def test_what_grok_answered_is_kept_without_the_key(self):
        self.fail(400, '{"error": "Model grok-4.7 not found for key xai-secret-key-0123456789"}')
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        [entry] = self.log()
        self.assertEqual((entry["status"], entry["message"]), ("error", "Сервис распознавания не принял фотографию"))
        self.assertIn("HTTP 400", entry["detail"])
        self.assertIn("not found", entry["detail"])
        logged = [row for (pk, sk), row in self.table.rows.items() if sk.startswith("RECOG#")]
        self.assertNotIn(self.KEY, json.dumps(logged, ensure_ascii=False, default=str))

    def test_an_empty_answer_says_why(self):
        self.answer({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "reasoning"}]})
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        [entry] = self.log()
        self.assertEqual(entry["status"], "error")
        self.assertIn("max_output_tokens", entry["detail"])

    def test_a_replaced_photo_is_skipped(self):
        lambda_app.finish_recognition("5", "owner@example.com", "boxes/5/thumb/" + "c" * 32 + ".jpg", "ru")
        [entry] = self.log()
        self.assertEqual((entry["status"], entry["message"]), ("skipped", "Фотографию заменили до распознавания"))

    def test_an_attempt_that_never_ended_shows_as_lost(self):
        self.table.put_item({"pk": "USER#owner@example.com", "sk": "RECOG#2026-01-01T00:00:00.000Z#00000000",
                             "number": "5", "status": "running", "started_at": "2026-01-01T00:00:00.000Z"})
        status, body = call("GET", "/api/settings/recognition-log", email="owner@example.com")
        [entry] = body["items"]
        self.assertEqual(entry["status"], "lost")
        self.assertEqual(lambda_app.translate("en", "Распознавание не завершилось: функцию остановили по времени или из-за сбоя"),
                         "Recognition did not finish: the function was stopped by its time limit or a failure")

    def test_each_user_sees_only_their_own_log(self):
        self.fail(500, "boom")
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        self.assertEqual(call("GET", "/api/settings/recognition-log", email="other@example.com")[1]["items"], [])

    def test_an_admin_sees_everyones_log(self):
        self.fail(500, "boom")
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        lambda_app.finish_recognition("5", "guest@example.com", self.THUMB, "ru")
        os.environ["ADMIN_GROUP"] = "admins"
        self.assertEqual(call("GET", "/api/admin/recognition-log", email="owner@example.com")[0], 403)
        status, body = call("GET", "/api/admin/recognition-log", email="boss@example.com", groups="[admins]")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(item["user"] for item in body["items"]), ["guest@example.com", "owner@example.com"])
        self.assertEqual(len(call("GET", "/api/boxes", email="owner@example.com")[1]["items"]), 1, "the box list stays boxes only")

    def test_entries_expire(self):
        self.fail(500, "boom")
        lambda_app.finish_recognition("5", "owner@example.com", self.THUMB, "ru")
        [row] = [row for (pk, sk), row in self.table.rows.items() if sk.startswith("RECOG#")]
        self.assertGreater(row["expires_at"], datetime.now(timezone.utc).timestamp() + 29 * 86400)


class HistoryDeleteTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", ADMIN_GROUP="admins")
        table = lambda_app._table = ScanTable([
            meta("7", "Лампы"),
            lambda_app.history_item("7", "", "Лампы", "2026-09-01T10:00:00.000Z", "a@example.com", ""),
            lambda_app.history_item("7", "", "Ошибка", "2026-09-02T10:00:00.000Z", "a@example.com", ""),
        ])

        def delete_item(Key, ConditionExpression=None, ReturnValues=None):
            if (Key["pk"], Key["sk"]) not in table.rows:
                raise ClientError("ConditionalCheckFailedException")
            return {"Attributes": table.rows.pop((Key["pk"], Key["sk"]))}

        table.delete_item = delete_item
        self.table = table

    def tearDown(self):
        lambda_app._table = None

    def ids(self):
        return [item["id"] for item in call("GET", "/api/boxes/7/history")[1]["items"]]

    def delete(self, entry_id, groups="[admins]"):
        return call("DELETE", "/api/boxes/7/history/" + entry_id, groups=groups)

    def test_an_admin_deletes_one_entry(self):
        newest, oldest = self.ids()
        self.assertTrue(newest.startswith("2026-09-02T10:00:00.000Z#"))
        self.assertEqual(self.delete(newest)[0], 200)
        self.assertEqual(self.ids(), [oldest])
        self.assertIn(("BOX#7", "META"), self.table.rows, "the card stays")

    def test_others_cannot_delete(self):
        entry = self.ids()[0]
        self.assertEqual(self.delete(entry, groups=None)[0], 403)
        self.assertEqual(self.delete(entry, groups="[readers]")[0], 403)
        self.assertEqual(len(self.ids()), 2)

    def test_unknown_or_malformed_entry(self):
        self.assertEqual(self.delete("2026-01-01T00:00:00.000Z#00000000")[0], 404)
        self.assertEqual(self.delete("META")[0], 400)
        self.assertEqual(self.delete("2026-01-01T00:00:00.000Z#0000000g")[0], 400)


class TurnedPhotoTest(unittest.TestCase):
    ORIGINAL = "boxes/5/original/" + "a" * 32 + ".jpg"
    THUMB = "boxes/5/thumb/" + "b" * 32 + ".jpg"

    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media")
        lambda_app._table = ScanTable([meta("5", "Кабели", "Синие провода")])
        self.written = []
        self.queued = []
        names = ("transact", "head_image", "cleanup_prefix", "recognition_configured", "queue_recognition")
        self.saved = {name: getattr(lambda_app, name) for name in names}
        lambda_app.transact = self.written.extend
        lambda_app.head_image = lambda key, limit: {"ContentType": "image/jpeg"}
        lambda_app.cleanup_prefix = lambda number, keep: None
        lambda_app.recognition_configured = lambda who: True
        lambda_app.queue_recognition = lambda *args: self.queued.append(args)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(lambda_app, name, value)
        lambda_app._table = None

    def complete(self, **extra):
        status, body = call("POST", "/api/boxes/5/photo/complete", dict(original_key=self.ORIGINAL, thumb_key=self.THUMB, **extra))
        self.assertEqual(status, 200)
        update = self.written[0]["Update"]["ExpressionAttributeValues"]
        return body, update[":h"]["S"], [action for action in self.written if "Put" in action]

    def fields(self, history):
        return [action["Put"]["Item"].get("field", {}).get("S") for action in history]

    def test_a_turned_photo_keeps_its_description(self):
        body, hidden, history = self.complete(keep_description=True)
        self.assertEqual((body["hidden_text"], hidden), ("Синие провода", "Синие провода"))
        self.assertFalse(body["recognition_pending"])
        self.assertEqual((self.queued, self.fields(history)), ([], []), "no history entry")

    def test_a_new_photo_is_recognized_again(self):
        body, hidden, history = self.complete()
        self.assertEqual((body["hidden_text"], hidden), ("", ""))
        self.assertTrue(body["recognition_pending"])
        self.assertEqual(self.fields(history), ["photo", "hidden_text"])


class SettingsTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)


class GrokKeyTest(unittest.TestCase):
    KEY = "xai-0123456789abcdefWXYZ"

    def setUp(self):
        os.environ.update(TABLE_NAME="boxes")
        self.table = lambda_app._table = SettingsTable()

    def tearDown(self):
        lambda_app._table = None

    def saved_key(self):
        return self.table.items[("USER#person@example.com", "SETTINGS")]["grok_api_key"]

    def test_the_key_never_leaves_the_api(self):
        status, body = call("PUT", "/api/settings", {"grok_api_key": self.KEY, "grok_model": "grok-4.7"})
        self.assertEqual(status, 200)
        self.assertNotIn(self.KEY, json.dumps(body))
        status, body = call("GET", "/api/settings")
        self.assertEqual(body, {"recognition": True, "grok_key_set": True, "grok_key_hint": "WXYZ", "grok_model": "grok-4.7",
                                "grok_prompt": "", "default_prompt": lambda_app.RECOGNITION_PROMPTS["ru"], "rerun": None})
        self.assertNotIn(self.KEY, json.dumps(body))

    def test_an_empty_key_keeps_the_saved_one(self):
        call("PUT", "/api/settings", {"grok_api_key": self.KEY, "grok_model": "grok-4.7"})
        call("PUT", "/api/settings", {"grok_api_key": "", "grok_model": "grok-5"})
        self.assertEqual(self.saved_key(), self.KEY)
        self.assertEqual(call("GET", "/api/settings")[1]["grok_model"], "grok-5")

    def test_remove_key_deletes_it(self):
        call("PUT", "/api/settings", {"grok_api_key": self.KEY, "grok_model": "grok-4.7"})
        status, body = call("PUT", "/api/settings", {"remove_key": True, "grok_model": "grok-4.7"})
        self.assertEqual(self.saved_key(), "")
        self.assertFalse(body["grok_key_set"])

    def test_an_own_prompt_is_kept_until_changed(self):
        call("PUT", "/api/settings", {"grok_api_key": self.KEY, "grok_model": "grok-4.7", "grok_prompt": "Только инструменты"})
        call("PUT", "/api/settings", {"grok_api_key": "", "grok_model": "grok-5"})
        self.assertEqual(call("GET", "/api/settings")[1]["grok_prompt"], "Только инструменты")
        call("PUT", "/api/settings", {"grok_model": "grok-5", "grok_prompt": ""})
        self.assertEqual(call("GET", "/api/settings")[1]["grok_prompt"], "", "empty is the built-in prompt")

    def test_the_prompt_gets_the_language(self):
        self.assertEqual(lambda_app.recognition_prompt({}, "en"), lambda_app.RECOGNITION_PROMPTS["en"])
        self.assertEqual(lambda_app.recognition_prompt({"grok_prompt": "Only tools. "}, "it"), "Only tools.\n\nScrivi in italiano.")

    def test_a_short_key_gets_no_hint(self):
        call("PUT", "/api/settings", {"grok_api_key": "short", "grok_model": "grok-4.7"})
        self.assertEqual(call("GET", "/api/settings")[1]["grok_key_hint"], "")


class ScanTable:
    """Rows in memory. The scan filter is left to the code under test."""

    def __init__(self, rows):
        self.rows = {(row["pk"], row["sk"]): row for row in rows}

    def scan(self, **kwargs):
        return {"Items": list(self.rows.values())}

    def get_item(self, Key):
        row = self.rows.get((Key["pk"], Key["sk"]))
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[(Item["pk"], Item["sk"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kwargs):
        # Only the created_at fill-in uses this.
        row = self.rows[(Key["pk"], Key["sk"])]
        row.setdefault("created_at", ExpressionAttributeValues[":c"])

    def query(self, KeyConditionExpression, IndexName=None, ScanIndexForward=False, Limit=None, **kwargs):
        if IndexName:
            name, value = KeyConditionExpression.get_expression()["values"]
            rows = [row for row in self.rows.values() if row.get(name.name) == value]
            rows = sorted(rows, key=lambda row: row.get("gsi1sk") or "", reverse=not ScanIndexForward)
        else:
            values = KeyConditionExpression.get_expression()["values"]
            if hasattr(values[0], "get_expression"):
                pk = values[0].get_expression()["values"][1]
                prefix = values[1].get_expression()["values"][1]
            else:
                pk, prefix = values[1], ""
            rows = [row for (key, sk), row in self.rows.items() if key == pk and sk.startswith(prefix)]
            rows = sorted(rows, key=lambda row: row["sk"], reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}

    def delete_item(self, Key, **kwargs):
        return {"Attributes": self.rows.pop((Key["pk"], Key["sk"]), None)}

    def batch_writer(self):
        table = self

        class Batch:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def delete_item(self, Key):
                table.rows.pop((Key["pk"], Key["sk"]), None)

        return Batch()


def meta(number, contents, hidden=""):
    return {"pk": "BOX#" + number, "sk": "META", "number": number, "gsi1pk": "BOX", "gsi1sk": lambda_app.sort_key(number),
            "description": "", "contents": contents, "hidden_text": hidden, "thumb_key": "",
            "search_text": lambda_app.build_search(number, "", contents, hidden), "version": 1}


class SortTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes")
        rows = []
        # Box 1: oldest, changed last. Box 2: no created_at, history says when.
        # Box 3: no created_at and no history. Box 10: newest.
        # Names: box 10 has none, so it goes last.
        for number, created, updated, name in (("1", "2026-01-01T00:00:00.000Z", "2026-09-30T00:00:00.000Z", "## Ёлочные **игрушки**"),
                                               ("2", None, "2026-03-01T00:00:00.000Z", "Инструменты"),
                                               ("3", None, "2026-05-01T00:00:00.000Z", "елка искусственная\nв чехле"),
                                               ("10", "2026-08-01T00:00:00.000Z", "2026-08-01T00:00:00.000Z", "")):
            row = dict(meta(number, "Кабели " + number), updated_at=updated, description=name)
            if created:
                row["created_at"] = created
            rows.append(row)
        rows.append(lambda_app.history_item("2", "", "Кабели 2", "2026-02-01T00:00:00.000Z", "a@example.com", ""))
        rows.append(lambda_app.history_item("2", "Кабели 2", "Кабели 2!", "2026-02-15T00:00:00.000Z", "a@example.com", ""))
        self.table = lambda_app._table = ScanTable(rows)

    def tearDown(self):
        lambda_app._table = None

    def numbers(self, **params):
        status, body = call("GET", "/api/boxes", params=params)
        self.assertEqual(status, 200)
        return [item["number"] for item in body["items"]], body["next_cursor"]

    def test_number_is_the_default(self):
        self.assertEqual(self.numbers()[0], ["1", "2", "3", "10"])
        self.assertEqual(self.numbers(sort="nonsense")[0], ["1", "2", "3", "10"])
        self.assertEqual(self.numbers(sort="number")[0], ["1", "2", "3", "10"])

    def test_by_name(self):
        # "елка искусственная" before "Ёлочные игрушки": case and ё do not count.
        self.assertEqual(self.numbers(sort="name")[0], ["3", "1", "2", "10"])

    def test_numbers_with_letters_sort_naturally(self):
        for number in ("A-10", "A-9", "12a", "B-1"):
            self.table.put_item(dict(meta(number, ""), updated_at="2026-01-01T00:00:00.000Z"))
        self.assertEqual(self.numbers()[0], ["1", "2", "3", "10", "12a", "A-9", "A-10", "B-1"])
        first, cursor = self.numbers(limit="5")
        rest, end = self.numbers(limit="5", cursor=cursor)
        self.assertEqual((first + rest, end), (["1", "2", "3", "10", "12a", "A-9", "A-10", "B-1"], None))

    def test_plain_title_matches_the_page(self):
        self.assertEqual(lambda_app.plain_title("\n## Electronics: **Raspberry Pi** [kit](https://x)\nmore"), "Electronics: Raspberry Pi kit")
        self.assertEqual(lambda_app.plain_title(None), "")

    def test_last_changed_first(self):
        self.assertEqual(self.numbers(sort="updated")[0], ["1", "10", "3", "2"])

    def test_last_added_first_with_dates_filled_in(self):
        self.assertEqual(self.numbers(sort="created")[0], ["10", "3", "2", "1"])
        self.assertEqual(self.table.rows[("BOX#2", "META")]["created_at"], "2026-02-01T00:00:00.000Z", "the first history entry")
        self.assertEqual(self.table.rows[("BOX#3", "META")]["created_at"], "2026-05-01T00:00:00.000Z", "the last change")
        status, body = call("GET", "/api/boxes", params={"sort": "created"})
        self.assertEqual(body["items"][0]["created_at"], "2026-08-01T00:00:00.000Z")

    def test_pages_follow_the_sort(self):
        first, cursor = self.numbers(sort="updated", limit="3")
        self.assertEqual(first, ["1", "10", "3"])
        rest, end = self.numbers(sort="updated", limit="3", cursor=cursor)
        self.assertEqual((rest, end), (["2"], None))

    def test_history_search_is_sorted_too(self):
        self.assertEqual(self.numbers(q="кабели", history="1", sort="updated")[0], ["1", "10", "3", "2"])


class HistorySearchTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes")
        lambda_app._table = ScanTable([
            meta("1", "Гирлянда"),
            lambda_app.history_item("1", "", "Гирлянда", "2026-09-01T10:00:00.000Z", "a@example.com", ""),
            meta("2", "Пусто"),
            lambda_app.history_item("2", "Старая Ёлка", "Пусто", "2026-09-02T10:00:00.000Z", "a@example.com", "Убрали"),
            meta("3", "Провода"),
            lambda_app.history_item("3", "", "", "2026-09-03T10:00:00.000Z", "a@example.com", "", field="hidden_text"),
        ])

    def tearDown(self):
        lambda_app._table = None

    def numbers(self, params):
        status, body = call("GET", "/api/boxes", params=params)
        self.assertEqual(status, 200)
        return {item["number"]: item.get("history_match", False) for item in body["items"]}

    def test_old_contents_are_found_only_when_asked(self):
        self.assertEqual(self.numbers({"q": "елка", "history": "1"}), {"2": True})

    def test_a_current_match_is_not_marked(self):
        self.assertEqual(self.numbers({"q": "гирлянда", "history": "1"}), {"1": False})

    def test_every_word_has_to_match_one_entry(self):
        self.assertEqual(self.numbers({"q": "елка убрали", "history": "1"}), {"2": True})
        self.assertEqual(self.numbers({"q": "елка гирлянда", "history": "1"}), {})


class HiddenTextHistoryTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", RECOGNITION="true")
        self.written = []
        self.table = lambda_app._table = ScanTable([dict(meta("5", "Кабели", "Старое описание"), thumb_key="boxes/5/thumb/new.jpg")])
        self.saved = (lambda_app.transact, lambda_app.describe_photo)
        lambda_app.transact = self.written.extend
        lambda_app.describe_photo = lambda user, key, lang, log=None: "Новое описание"

    def tearDown(self):
        lambda_app.transact, lambda_app.describe_photo = self.saved
        lambda_app._table = None

    def puts(self):
        from boto3.dynamodb.types import TypeDeserializer

        load = TypeDeserializer().deserialize
        return [{key: load(value) for key, value in action["Put"]["Item"].items()} for action in self.written if "Put" in action]

    def test_recognition_writes_a_history_entry(self):
        lambda_app.finish_recognition("5", "owner@example.com", "boxes/5/thumb/new.jpg", "ru")
        [entry] = self.puts()
        self.assertEqual(entry["field"], "hidden_text")
        self.assertEqual((entry["contents_before"], entry["contents_after"]), ("Старое описание", "Новое описание"))
        self.assertEqual(entry["changed_by"], "owner@example.com")

    def test_a_stale_photo_writes_nothing(self):
        lambda_app.finish_recognition("5", "owner@example.com", "boxes/5/thumb/old.jpg", "ru")
        self.assertEqual(self.written, [])

    def test_history_says_which_field_changed(self):
        self.table.rows[("BOX#5", "HIST#1")] = lambda_app.history_item("5", "a", "b", "2026-09-01T10:00:00.000Z", "x@example.com", "")
        self.table.rows[("BOX#5", "HIST#2")] = lambda_app.history_item("5", "", "c", "2026-09-02T10:00:00.000Z", "x@example.com", "", field="hidden_text")
        self.table.rows = {(row["pk"], row["sk"]): row for row in self.table.rows.values()}
        status, body = call("GET", "/api/boxes/5/history")
        self.assertEqual(status, 200)
        self.assertEqual([item["field"] for item in body["items"]], ["hidden_text", "contents"])


class SigningTest(unittest.TestCase):
    def test_signature_matches_openssl(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = openssl_key(tmp, "genrsa", "-traditional", "2048")
            with open(key_path) as handle:
                pem = handle.read()
            message = b'{"Statement":[]}'
            expected = subprocess.run(
                ["openssl", "dgst", "-sha1", "-sign", key_path],
                input=message, check=True, capture_output=True,
            ).stdout
            self.assertEqual(lambda_app.rsa_sha1_sign(message, lambda_app.load_rsa_private_key(pem)), expected)

    def test_pkcs8_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(openssl_key(tmp, "genpkey", "-algorithm", "RSA")) as handle:
                key = lambda_app.load_rsa_private_key(handle.read())
            self.assertEqual(key["p"] * key["q"], key["n"])

    def test_cookies_cover_media_in_cloudfront_alphabet(self):
        os.environ["CLOUDFRONT_KEY_PAIR_ID"] = "K123"
        with tempfile.TemporaryDirectory() as tmp:
            with open(openssl_key(tmp, "genrsa", "-traditional", "2048")) as handle:
                lambda_app._signing_key = lambda_app.load_rsa_private_key(handle.read())
        cookies = lambda_app.media_cookies("boxes.example.com")
        policy = cookies[0].split(";")[0].split("=", 1)[1]
        self.assertNotRegex(policy, r"[+=/]")
        decoded = base64.b64decode(policy.replace("-", "+").replace("_", "=").replace("~", "/"))
        self.assertIn(b"https://boxes.example.com/media/*", decoded)
        self.assertTrue(all("Path=/media" in cookie for cookie in cookies))
        self.assertTrue(cookies[2].startswith("CloudFront-Key-Pair-Id=K123;"))


def version_route():
    result = lambda_app.route("GET", ["version"], {"requestContext": {}})
    return result["statusCode"], json.loads(result["body"])


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <updated>2026-10-14T09:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Tozapid/Example/releases/tag/v1.10.0"/>
    <title>v1.10.0 — 2026-10-14 — versions</title>
    <content type="html">&lt;h3&gt;Added&lt;/h3&gt;
&lt;ul&gt;
&lt;li&gt;Settings → &lt;code&gt;Version&lt;/code&gt; shows the &lt;a href="x"&gt;releases&lt;/a&gt;.&lt;/li&gt;
&lt;li&gt;Two&lt;/li&gt;
&lt;/ul&gt;</content>
  </entry>
  <entry>
    <updated>2026-10-13T09:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Tozapid/Example/releases/tag/nightly"/>
    <title>nightly</title>
  </entry>
  <entry>
    <updated>2026-10-12T09:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Tozapid/Example/releases/tag/v1.9.0"/>
    <title>v1.9.0 — 2026-10-12</title>
  </entry>
  <entry>
    <updated>2026-10-13T08:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Tozapid/Example/releases/tag/v1.9.1"/>
    <title>v1.9.1 — 2026-10-13 — a fix</title>
  </entry>
</feed>""".encode()


class FakeFeed:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body


# The releases of the demo repository from the Atom feed of GitHub, without
# GitHub: urlopen is replaced.
class VersionTest(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in ("VERSION", "RELEASES_REPOSITORY")}
        os.environ.update(VERSION="v1.9.0", RELEASES_REPOSITORY="Tozapid/Example")
        self.urls = []
        self.feed = FEED
        real = lambda_app.urllib.request.urlopen
        lambda_app.urllib.request.urlopen = self.urlopen
        self.addCleanup(setattr, lambda_app.urllib.request, "urlopen", real)
        lambda_app._releases = None
        self.addCleanup(setattr, lambda_app, "_releases", None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def urlopen(self, request, timeout=None):
        self.urls.append(request.full_url)
        if isinstance(self.feed, Exception):
            raise self.feed
        return FakeFeed(self.feed)

    def test_the_releases_after_this_version_newest_first(self):
        status, info = version_route()
        self.assertEqual(status, 200)
        self.assertEqual(self.urls, ["https://github.com/Tozapid/Example/releases.atom"])
        self.assertEqual((info["version"], info["repository"], info["latest"], info["error"]), ("v1.9.0", "Tozapid/Example", "v1.10.0", None))
        self.assertEqual(info["url"], "https://github.com/Tozapid/Example/releases")
        self.assertEqual([release["version"] for release in info["newer"]], ["v1.10.0", "v1.9.1"], "by number, not by text; no tag without a version")
        newest = info["newer"][0]
        self.assertEqual((newest["title"], newest["date"], newest["url"]), ("v1.10.0 — 2026-10-14 — versions", "2026-10-14", "https://github.com/Tozapid/Example/releases/tag/v1.10.0"))
        self.assertEqual(newest["notes"], "Added\n• Settings → Version shows the releases.\n• Two")
        self.assertTrue(info["checked_at"].endswith("Z"))

    def test_the_newest_version_has_nothing_newer(self):
        os.environ["VERSION"] = "v1.10.0"
        info = lambda_app.version_info()
        self.assertEqual((info["latest"], info["newer"]), ("v1.10.0", []))

    def test_the_list_stays_an_hour(self):
        lambda_app.version_info()
        lambda_app.version_info()
        self.assertEqual(len(self.urls), 1)
        repository, read_at, releases, checked_at = lambda_app._releases
        lambda_app._releases = (repository, read_at - lambda_app.RELEASES_SECONDS - 1, releases, checked_at)
        lambda_app.version_info()
        self.assertEqual(len(self.urls), 2)

    def test_without_github_the_last_list_serves(self):
        self.feed = OSError("no network")
        info = lambda_app.version_info()
        self.assertEqual((info["version"], info["error"], info["newer"]), ("v1.9.0", "unavailable", []))
        self.feed = FEED
        lambda_app.version_info()
        repository, read_at, releases, checked_at = lambda_app._releases
        lambda_app._releases = (repository, read_at - lambda_app.RELEASES_SECONDS - 1, releases, checked_at)
        self.feed = b"<html>not a feed"
        info = lambda_app.version_info()
        self.assertEqual((info["error"], info["latest"], info["checked_at"]), (None, "v1.10.0", checked_at))

    def test_without_a_repository_nothing_is_asked(self):
        os.environ["RELEASES_REPOSITORY"] = ""
        info = lambda_app.version_info()
        self.assertEqual((info["version"], info["url"], info["latest"], info["error"]), ("v1.9.0", None, None, None))
        self.assertEqual(self.urls, [])


if __name__ == "__main__":
    unittest.main()


class FakeBucket:
    def __init__(self, keys):
        self.keys = set(keys)

    def get_paginator(self, name):
        bucket = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": key} for key in sorted(bucket.keys) if key.startswith(Prefix)]}]

        return Paginator()

    def delete_object(self, Bucket, Key):
        self.keys.discard(Key)


class PhotoHistoryTest(unittest.TestCase):
    OLD = ("boxes/5/original/" + "a" * 32 + ".jpg", "boxes/5/thumb/" + "a" * 32 + ".jpg")
    NEW = ("boxes/5/original/" + "b" * 32 + ".jpg", "boxes/5/thumb/" + "b" * 32 + ".jpg")
    LOST = "boxes/5/original/" + "c" * 32 + ".jpg"

    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media", ADMIN_GROUP="admins")
        self.table = lambda_app._table = ScanTable([dict(meta("5", "Кабели"), photo_key=self.OLD[0], thumb_key=self.OLD[1])])
        self.bucket = lambda_app._s3 = FakeBucket(self.OLD + self.NEW + (self.LOST,))
        self.saved = (lambda_app.transact, lambda_app.head_image, lambda_app.recognition_configured)
        lambda_app.transact = self.apply
        lambda_app.head_image = lambda key, limit: {"ContentType": "image/jpeg"}
        lambda_app.recognition_configured = lambda who: False

    def tearDown(self):
        lambda_app.transact, lambda_app.head_image, lambda_app.recognition_configured = self.saved
        lambda_app._table = None
        lambda_app._s3 = None

    def apply(self, actions):
        from boto3.dynamodb.types import TypeDeserializer

        load = TypeDeserializer().deserialize
        for action in actions:
            if "Put" in action:
                self.table.put_item({key: load(value) for key, value in action["Put"]["Item"].items()})
            elif "Update" in action:
                update = action["Update"]
                row = self.table.rows[(load(update["Key"]["pk"]), load(update["Key"]["sk"]))]
                values = {key: load(value) for key, value in update["ExpressionAttributeValues"].items()}
                if row["sk"] == "META":
                    row.update(photo_key=values[":p"], thumb_key=values[":t"])
                else:
                    for part in update["UpdateExpression"].removeprefix("SET ").split(", "):
                        name, value = part.split(" = ")
                        row[name] = values[value]

    def test_the_old_photo_stays_and_the_history_shows_both(self):
        status, _ = call("POST", "/api/boxes/5/photo/complete", {"original_key": self.NEW[0], "thumb_key": self.NEW[1]})
        self.assertEqual(status, 200)
        self.assertEqual(self.bucket.keys, set(self.OLD + self.NEW), "an unfinished upload goes, the old photo stays")
        [entry] = call("GET", "/api/boxes/5/history")[1]["items"]
        self.assertEqual(entry["field"], "photo")
        self.assertEqual((entry["before_photo_url"], entry["after_thumb_url"]), ("/media/" + self.OLD[0], "/media/" + self.NEW[1]))

    def test_a_turned_photo_replaces_the_old_one_without_an_entry(self):
        call("POST", "/api/boxes/5/photo/complete", {"original_key": self.NEW[0], "thumb_key": self.NEW[1]})
        turned = ("boxes/5/original/" + "d" * 32 + ".jpg", "boxes/5/thumb/" + "d" * 32 + ".jpg")
        self.bucket.keys.update(turned)
        status, _ = call("POST", "/api/boxes/5/photo/complete", {"original_key": turned[0], "thumb_key": turned[1], "keep_description": True})
        self.assertEqual(status, 200)
        [entry] = call("GET", "/api/boxes/5/history")[1]["items"]
        self.assertEqual((entry["before_photo_url"], entry["after_thumb_url"]), ("/media/" + self.OLD[0], "/media/" + turned[1]), "the entry shows the turned photo")
        self.assertEqual(self.bucket.keys, set(self.OLD + turned), "the photo before the turn is gone")

    def test_deleting_the_entry_deletes_a_photo_nothing_shows(self):
        call("POST", "/api/boxes/5/photo/complete", {"original_key": self.NEW[0], "thumb_key": self.NEW[1]})
        [entry] = call("GET", "/api/boxes/5/history")[1]["items"]
        self.assertEqual(call("DELETE", "/api/boxes/5/history/" + entry["id"], groups="[admins]")[0], 200)
        self.assertEqual(self.bucket.keys, set(self.NEW))


class TrashTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media", ADMIN_GROUP="admins")
        photo = "boxes/7/original/" + "a" * 32 + ".jpg"
        self.table = lambda_app._table = ScanTable([
            dict(meta("7", "Лампы"), photo_key=photo, description="Свет"),
            lambda_app.history_item("7", "", "Лампы", "2026-09-01T10:00:00.000Z", "a@example.com", ""),
        ])
        self.bucket = lambda_app._s3 = FakeBucket([photo])
        self.saved = lambda_app.transact
        lambda_app.transact = self.apply

    def tearDown(self):
        lambda_app.transact = self.saved
        lambda_app._table = None
        lambda_app._s3 = None

    def apply(self, actions):
        from boto3.dynamodb.types import TypeDeserializer

        load = TypeDeserializer().deserialize
        for action in actions:
            if "Put" in action:
                item = {key: load(value) for key, value in action["Put"]["Item"].items()}
                if (item["pk"], item["sk"]) in self.table.rows and "attribute_not_exists" in action["Put"].get("ConditionExpression", ""):
                    raise ClientError("ConditionalCheckFailedException")
                self.table.put_item(item)
            elif "Delete" in action:
                key = action["Delete"]["Key"]
                self.table.rows.pop((load(key["pk"]), load(key["sk"])), None)

    def numbers(self):
        return [item["number"] for item in call("GET", "/api/boxes")[1]["items"]]

    def test_a_deleted_box_waits_in_the_trash(self):
        self.assertEqual(call("DELETE", "/api/boxes/7")[0], 200)
        self.assertEqual(self.numbers(), [])
        self.assertEqual(call("GET", "/api/boxes/7")[0], 404)
        [item] = call("GET", "/api/trash")[1]["items"]
        self.assertEqual((item["number"], item["description"], item["deleted_by"]), ("7", "Свет", "person@example.com"))
        self.assertTrue(self.bucket.keys, "the photo stays")
        self.assertTrue(any(sk.startswith("HIST#") for pk, sk in self.table.rows), "the history stays")

    def test_the_number_stays_taken_while_in_the_trash(self):
        call("DELETE", "/api/boxes/7")
        status, body = call("POST", "/api/boxes", {"number": "7"})
        self.assertEqual(status, 409)
        self.assertIn("корзине", body["error"])

    def test_a_box_is_restored_with_its_history(self):
        call("DELETE", "/api/boxes/7")
        status, body = call("POST", "/api/trash/7/restore")
        self.assertEqual((status, body["description"]), (200, "Свет"))
        self.assertEqual(self.numbers(), ["7"])
        self.assertEqual(call("GET", "/api/trash")[1]["items"], [])
        self.assertEqual(len(call("GET", "/api/boxes/7/history")[1]["items"]), 1)
        self.assertNotIn("deleted_at", self.table.rows[("BOX#7", "META")])

    def test_only_an_admin_purges(self):
        call("DELETE", "/api/boxes/7")
        self.assertEqual(call("DELETE", "/api/trash/7")[0], 403)
        self.assertEqual(call("DELETE", "/api/trash/7", groups="[admins]")[0], 200)
        self.assertEqual((self.table.rows, self.bucket.keys), ({}, set()))
        self.assertEqual(call("POST", "/api/trash/7/restore")[0], 404)


class DraftTest(unittest.TestCase):
    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", MEDIA_BUCKET="media")
        self.table = lambda_app._table = ScanTable([dict(meta("7", "Лампы"), description="Свет")])
        self.saved = lambda_app.transact
        lambda_app.transact = self.apply

    def tearDown(self):
        lambda_app.transact = self.saved
        lambda_app._table = None

    def apply(self, actions):
        from boto3.dynamodb.types import TypeDeserializer

        load = TypeDeserializer().deserialize
        for action in actions:
            if "Put" in action:
                self.table.put_item({key: load(value) for key, value in action["Put"]["Item"].items()})
            elif "Delete" in action:
                key = action["Delete"]["Key"]
                self.table.rows.pop((load(key["pk"]), load(key["sk"])), None)

    def drafts(self, email="person@example.com"):
        return call("GET", "/api/drafts", email=email)[1]["items"]

    def test_a_draft_is_kept_as_typed_for_its_user_alone(self):
        status, draft = call("PUT", "/api/drafts/boxes/7", {"description": "Свет ", "contents": "- лампа\n", "note": "", "version": 1})
        self.assertEqual((status, draft["number"], draft["contents"], draft["version"]), (200, "7", "- лампа\n", 1))
        call("PUT", "/api/drafts/new", {"number": "8", "description": "Новая"})
        self.assertEqual(sorted((item["new"], item["number"], item["description"]) for item in self.drafts()), [(False, "7", "Свет "), (True, "8", "Новая")])
        self.assertEqual(self.drafts("other@example.com"), [])

    def test_saving_the_card_deletes_its_draft(self):
        call("PUT", "/api/drafts/boxes/7", {"description": "Свет и лампы", "version": 1})
        status, _ = call("PATCH", "/api/boxes/7", {"description": "Свет и лампы", "contents": "Лампы", "version": 1})
        self.assertEqual((status, self.drafts()), (200, []))

    def test_creating_a_box_deletes_the_new_box_draft(self):
        call("PUT", "/api/drafts/new", {"number": "8"})
        self.assertEqual(call("POST", "/api/boxes", {"number": "8"})[0], 201)
        self.assertEqual(self.drafts(), [])

    def test_a_draft_is_deleted_and_checked(self):
        call("PUT", "/api/drafts/boxes/7", {"contents": "x"})
        self.assertEqual(call("DELETE", "/api/drafts/boxes/7")[0], 200)
        self.assertEqual(self.drafts(), [])
        self.assertEqual(call("PUT", "/api/drafts/boxes/a b", {})[0], 400)
        self.assertEqual(call("PUT", "/api/drafts/new", {"contents": "x" * 20001})[0], 400)


class RerunTest(unittest.TestCase):
    THUMB = "boxes/{}/thumb/" + "b" * 32 + ".jpg"

    def setUp(self):
        os.environ.update(TABLE_NAME="boxes", RECOGNITION="true")
        self.table = lambda_app._table = ScanTable([
            dict(meta("1", "Кабели"), thumb_key=self.THUMB.format(1)),
            meta("2", "Без фото"),
            dict(meta("3", "Лампы"), thumb_key=self.THUMB.format(3)),
            {"pk": "USER#person@example.com", "sk": "SETTINGS", "grok_api_key": "xai-key", "grok_model": "grok-4.7"},
        ])
        self.queued = []
        self.recognized = []
        self.saved = (lambda_app.queue_rerun, lambda_app.finish_recognition)
        lambda_app.queue_rerun = lambda *args: self.queued.append(args)
        lambda_app.finish_recognition = lambda number, user, thumb, lang, note="": self.recognized.append((number, lang, note))

    def tearDown(self):
        lambda_app.queue_rerun, lambda_app.finish_recognition = self.saved
        lambda_app._table = None

    def run_row(self):
        return self.table.rows[("USER#person@example.com", "RERUN")]

    def test_every_photo_is_recognized_again(self):
        status, body = call("POST", "/api/recognition/rerun", {"lang": "en"})
        self.assertEqual((status, body["rerun"]["total"], body["rerun"]["status"]), (200, 2, "running"))
        [(user, run, numbers, lang)] = self.queued
        self.assertEqual((user, numbers, lang), ("person@example.com", ["1", "3"], "en"))
        lambda_app.recognize_all(user, run, numbers, lang)
        self.assertEqual(self.recognized, [("1", "en", "Повторное распознавание"), ("3", "en", "Повторное распознавание")])
        self.assertEqual((self.run_row()["status"], self.run_row()["done"]), ("done", 2))
        self.assertEqual(call("GET", "/api/settings")[1]["rerun"]["done"], 2)

    def test_a_stopped_run_ends(self):
        call("POST", "/api/recognition/rerun", {"lang": "ru"})
        [(user, run, numbers, lang)] = self.queued
        self.assertEqual(call("DELETE", "/api/recognition/rerun")[1]["rerun"]["status"], "stopped")
        lambda_app.recognize_all(user, run, numbers, lang)
        self.assertEqual(self.recognized, [])

    def test_the_rest_goes_to_a_new_call_when_time_runs_out(self):
        call("POST", "/api/recognition/rerun", {"lang": "ru"})
        [(user, run, numbers, lang)] = self.queued

        class Clock:
            left = [120_000, 30_000]

            def get_remaining_time_in_millis(self):
                return self.left.pop(0)

        lambda_app.recognize_all(user, run, numbers, lang, Clock())
        self.assertEqual(self.recognized, [("1", "ru", "Повторное распознавание")])
        self.assertEqual(self.queued[-1], (user, run, ["3"], "ru"))
        self.assertEqual(self.run_row()["status"], "running")

    def test_without_a_key_nothing_starts(self):
        del self.table.rows[("USER#person@example.com", "SETTINGS")]
        self.assertEqual(call("POST", "/api/recognition/rerun", {"lang": "ru"})[0], 400)
        self.assertEqual(self.queued, [])
